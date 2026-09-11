import os
import sys
import json
import time  # [MÉTRICAS] cronometrar cada paciente
import shutil
from datetime import datetime

# Console do Windows usa cp1252: textos de laudo (que começam com BOM ﻿ ou
# trazem acentos) derrubavam o print e, por tabela, o processamento do exame.
# UTF-8 + errors="replace" garante que nenhum print volte a quebrar a execução.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from core import (
    read_patients_from_gsheets,
    update_sheet_status,
    process_patient_exams,
    do_login,
    reset_para_busca,
)
from report_manager import ReportManager, TELEMETRIA_DIR
from config import (
    DOWNLOAD_DIR, HISTORY_FILE, HEADLESS_MODE, IS_DOCKER, REPORTS_DIR, RESULTS_S3_URI,
    DEBUG_DEDUP, RESET_HISTORICO,
)

load_dotenv()
SHEET_URL = os.getenv("SHEET_URL")


class _TeeStdout:
    """[DIAGNÓSTICO] Espelha o stdout num arquivo dentro de REPORTS_DIR, para o log do
    modo DEBUG_DEDUP subir ao S3 junto dos relatórios (o laço de _subir_resultados_s3 já
    varre essa pasta — nenhum upload novo é necessário). O CloudWatch continua recebendo
    tudo, porque cada write vai para os dois destinos."""

    def __init__(self, original, path):
        self._orig = original
        self._arq = open(path, "w", encoding="utf-8", errors="replace")

    def write(self, texto):
        self._orig.write(texto)
        try:
            self._arq.write(texto)
            self._arq.flush()  # o upload lê o arquivo no fim da run; sem buffer pendente
        except Exception:
            pass
        return len(texto)

    def flush(self):
        self._orig.flush()
        try:
            self._arq.flush()
        except Exception:
            pass

    def isatty(self):
        return False

    @property
    def encoding(self):
        return getattr(self._orig, "encoding", "utf-8")


def _ativar_log_arquivo(run_id):
    """[DIAGNÓSTICO] Só no modo DEBUG_DEDUP: duplica o stdout em
    REPORTS_DIR/debug_<run_id>.log. Devolve o caminho, ou None se não ativou."""
    if not DEBUG_DEDUP:
        return None
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        path = os.path.join(REPORTS_DIR, f"debug_{run_id}.log")
        sys.stdout = _TeeStdout(sys.stdout, path)
        print(f"[DEBUG_DEDUP] log desta execução: {path}")
        return path
    except Exception as e:
        print(f"AVISO: nao consegui abrir o log de diagnostico: {e}")
        return None


def _resetar_historico():
    """[DIAGNÓSTICO] Zera o histórico antes da run (só com RESET_HISTORICO=1), para que
    todo exame seja reavaliado e o caminho da dedup realmente execute. Sempre guarda um
    .bak ao lado no EFS antes de zerar. Best-effort: falha aqui não derruba a execução."""
    if not RESET_HISTORICO:
        return
    try:
        antes, bak = 0, None
        if os.path.isfile(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, encoding="utf-8") as f:
                    antes = len(json.load(f))
            except Exception:
                antes = -1  # ilegível: ainda assim faz backup antes de sobrescrever
            bak = f"{HISTORY_FILE}.bak-{datetime.now():%Y%m%d-%H%M%S}"
            shutil.copy(HISTORY_FILE, bak)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write("[]")
        print(f"[RESET_HISTORICO] historico zerado ({antes} -> 0 entradas)"
              + (f" | backup: {bak}" if bak else " | nao havia arquivo anterior"))
    except Exception as e:
        print(f"AVISO: falha ao resetar o historico: {e}")


def _subir_resultados_s3(uri):
    """Sobe downloads + telemetria + reports + histórico para o S3
    (uri = s3://bucket/prefixo), para o grupo conferir os laudos e rodar as métricas sem
    acessar o EFS. Best-effort: falha no upload NÃO derruba a execução.

    Os PDFs de laudo VÃO para o S3 (decisão do grupo, para conferência dos exames
    baixados). O bucket é privado (Block Public Access nos 4 itens) e criptografado
    (AES256) - é o controle que sustenta esses dados de paciente estarem lá."""
    try:
        import glob
        import boto3
        from urllib.parse import urlparse
        parsed = urlparse(uri)
        bucket, prefixo = parsed.netloc, parsed.path.strip("/")
        s3 = boto3.client("s3")
        enviados = 0
        # DOWNLOAD_DIR entra junto: os PDFs sobem para rpa/downloads/ a cada execução.
        # O staging .tmp/ fica de fora (glob "*" não pega oculto, e só arquivo é enviado).
        for base in (DOWNLOAD_DIR, TELEMETRIA_DIR, REPORTS_DIR):
            nome_base = os.path.basename(base.rstrip("/\\"))
            for arq in glob.glob(os.path.join(base, "*")):
                if os.path.isfile(arq):
                    key = "/".join(p for p in (prefixo, nome_base, os.path.basename(arq)) if p)
                    s3.upload_file(arq, bucket, key)
                    enviados += 1
        # Histórico: arquivo ÚNICO de estado (quais exames já foram baixados). Sobe sempre
        # com o mesmo nome, então o S3 espelha o estado atual do EFS em vez de acumular
        # cópias. É o que permite auditar/restaurar o histórico sem montar o EFS.
        if os.path.isfile(HISTORY_FILE):
            key = "/".join(p for p in (prefixo, "historico",
                                       os.path.basename(HISTORY_FILE)) if p)
            s3.upload_file(HISTORY_FILE, bucket, key)
            enviados += 1
        print(f"Resultados enviados ao S3 ({uri}): {enviados} arquivo(s)")
    except Exception as e:
        print(f"AVISO: falha ao enviar resultados ao S3: {e}")


def run_automation():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            f.write("[]")

    print(f"Inicio pipeline para extração de exames")

    report = ReportManager()
    _ativar_log_arquivo(report.run_id)  # [DIAGNÓSTICO] no-op fora do modo DEBUG_DEDUP
    _resetar_historico()                # [DIAGNÓSTICO] no-op sem RESET_HISTORICO=1
    report.migrar_pendentes_legados()

    result_data = read_patients_from_gsheets(SHEET_URL)
    if "error" in result_data:
        print(f"ERRO: Falha ao ler a planilha: {result_data['error']}")
        report.salvar_erros()
        return

    pacientes = result_data.get("pacientes", [])
    if not pacientes:
        print("Fila vazia no Google Sheets (Nenhum paciente com STATUS 1)")
        report.salvar_erros()
        path_final, conteudo = report.gerar_relatorio_final()
        print(conteudo)
        print(f"Relatório final salvo em: {path_final}")
        return

    print(f"{len(pacientes)} paciente(s) para processar")

    with sync_playwright() as p:
        # --disable-blink-features=AutomationControlled reduz detecção de bot.
        # No Docker, --no-sandbox + --disable-dev-shm-usage evitam crash do
        # Chromium em container (rodando como root e com /dev/shm pequeno).
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if IS_DOCKER:
            launch_args += ["--no-sandbox", "--disable-setuid-sandbox",
                            "--disable-dev-shm-usage", "--disable-gpu"]
        browser = p.chromium.launch(headless=HEADLESS_MODE, args=launch_args)
        # Viewport ALTO: a lista de resultados da busca renderiza só as linhas que
        # cabem na janela (com 720px cabiam ~15, escondendo o 16º/17º paciente e
        # provocando "não encontrado"). ~2200px comporta ~40 linhas -> todas
        # renderizam juntas (equivale a "tirar o zoom" no portal).
        context = browser.new_context(accept_downloads=True,
                                      viewport={"width": 1280, "height": 2200})
        page = context.new_page()

        try:
            do_login(page)
            report.registrar_login(True)  # [MÉTRICAS]
        except Exception as e:
            report.registrar_login(False)  # [MÉTRICAS]
            print(f"ERRO: Falha no login inicial: {e}")
            browser.close()
            report.salvar_erros()
            report.salvar_telemetria()  # [MÉTRICAS]
            path_final, conteudo = report.gerar_relatorio_final()
            print(conteudo)
            print(f"Relatório final salvo em: {path_final}")
            return

        try:
            for p_info in pacientes:
                nome, cpf, row_idx = p_info["nome"], p_info["cpf"], p_info["sheet_row"]
                print("=" * 60)

                _t0 = time.perf_counter()  # [MÉTRICAS]
                stats = process_patient_exams(page, context, nome, cpf, report)
                report.registrar_tempo_paciente(nome, time.perf_counter() - _t0)  # [MÉTRICAS]

                if stats["status"] != "not_found":  # [MÉTRICAS] download completo = sem falhas nos alvos
                    report.registrar_download_completo(nome, stats["falhas"] == 0)

                if stats["status"] == "success" and stats["falhas"] == 0:
                    print(f"RPA concluiu todos os exames de {nome}")
                    update_sheet_status(SHEET_URL, row_idx, 0)
                elif stats["status"] == "not_found":
                    print(f"Paciente {nome} não encontrado no portal — marcando STATUS 0")
                    update_sheet_status(SHEET_URL, row_idx, 0)
                else:
                    print(f"Paciente {nome} terminou com status={stats['status']} (falhas: {stats['falhas']})")

                try:
                    # Reset falho deixa a tela no estado da paciente anterior e a
                    # busca seguinte lê a tabela velha -> "não encontrada" falso.
                    if not reset_para_busca(page):
                        print(f"AVISO: reset para busca NAO confirmado após {nome} — "
                              f"a próxima busca pode ler a tabela dela")
                except Exception as e:
                    print(f"AVISO: Falha ao resetar para busca após {nome}: {e}")
        finally:
            try:
                browser.close()
            except Exception:
                pass

    path_erros = report.salvar_erros()
    path_tele = report.salvar_telemetria()  # [MÉTRICAS]
    path_final, conteudo = report.gerar_relatorio_final()
    print("\n" + conteudo)
    print(f"Relatório final salvo em: {path_final}")
    print(f"Relatório de erros salvo em: {path_erros}")
    print(f"Telemetria de métricas salva em: {path_tele}")  # [MÉTRICAS]

    if RESULTS_S3_URI:  # deploy AWS: sobe telemetria+reports pro S3 (grupo baixa lá)
        _subir_resultados_s3(RESULTS_S3_URI)


if __name__ == "__main__":
    run_automation()
