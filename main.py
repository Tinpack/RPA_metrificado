import os
import sys
import time  # [MÉTRICAS] cronometrar cada paciente

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
from report_manager import ReportManager
from config import DOWNLOAD_DIR, HISTORY_FILE, HEADLESS_MODE, IS_DOCKER

load_dotenv()
SHEET_URL = os.getenv("SHEET_URL")


def run_automation():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
            f.write("[]")

    print(f"Inicio pipeline para extração de exames")

    report = ReportManager()
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
                    reset_para_busca(page)
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


if __name__ == "__main__":
    run_automation()
