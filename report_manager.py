import os
import json
import time
from datetime import datetime
from config import REPORTS_DIR

PENDENTES_IA_LEGACY_FILE = os.path.abspath("./pendentes_ia.json")

# [MÉTRICAS] Pasta onde a telemetria bruta de cada execução é gravada
# (consumida depois por calcular_metricas.py). Mantida separada de REPORTS_DIR
# para não misturar com os relatórios operacionais existentes.
TELEMETRIA_DIR = os.path.abspath(os.getenv("TELEMETRIA_DIR", "./metricas/telemetria"))


class ReportManager:
    def __init__(self):
        self.timestamp = datetime.now()
        self._start = time.perf_counter()
        self._end = None
        self.erros = []
        self.por_paciente = {}
        self.metodos_download = {}
        self.pacientes_processados = 0
        self.pacientes_nao_encontrados = 0
        # [MÉTRICAS] Identificador da execução e campos da telemetria (Blocos 1 e 3).
        self.run_id = self.timestamp.strftime("%d-%m-%Y_%H%M%S")
        self.login_ok = None
        self.exames_eventos = []  # universo de exames vistos (necessário p/ FN/VN)
        os.makedirs(REPORTS_DIR, exist_ok=True)

    def _slot(self, paciente):
        if paciente not in self.por_paciente:
            # [MÉTRICAS] busca_ok / download_completo_ok / tempo_s adicionados ao slot existente.
            self.por_paciente[paciente] = {
                "alvos": 0, "baixados": 0, "falhas": 0, "ignorados": 0,
                "busca_ok": None, "download_completo_ok": None, "tempo_s": None,
            }
        return self.por_paciente[paciente]

    # [MÉTRICAS] --- Captura de telemetria (Bloco 3: etapas; Bloco 1: exames) ---
    def registrar_login(self, ok: bool):
        self.login_ok = bool(ok)

    def registrar_busca(self, paciente, encontrado: bool):
        self._slot(paciente)["busca_ok"] = bool(encontrado)

    def registrar_download_completo(self, paciente, ok: bool):
        self._slot(paciente)["download_completo_ok"] = bool(ok)

    def registrar_tempo_paciente(self, paciente, segundos: float):
        self._slot(paciente)["tempo_s"] = round(float(segundos), 2)

    def registrar_exame_visto(self, *, paciente, cpf, data_exame, nome_exame,
                              decisao, metodo=None):
        # Um evento por exame encontrado no portal, com a decisão do RPA. O
        # universo completo (inclusive ignorados) é o que permite calcular
        # FN/VN no refino, comparando contra o gabarito de autorização.
        self.exames_eventos.append({
            "paciente": paciente,
            "cpf": cpf,
            "data_exame": data_exame,
            "nome_exame": nome_exame,
            "decisao": decisao,
            "metodo": metodo,
        })

    def atualizar_decisao_exame(self, *, paciente, data_exame, nome_exame,
                                decisao, metodo=None):
        # Atualiza a decisão final de um exame-alvo já registrado (baixado,
        # falha_extracao, etc.). Se não existir, cria.
        for ev in reversed(self.exames_eventos):
            if (ev["paciente"] == paciente and ev["data_exame"] == data_exame
                    and ev["nome_exame"] == nome_exame):
                ev["decisao"] = decisao
                if metodo is not None:
                    ev["metodo"] = metodo
                return
        self.registrar_exame_visto(
            paciente=paciente, cpf="", data_exame=data_exame,
            nome_exame=nome_exame, decisao=decisao, metodo=metodo,
        )

    def registrar_alvos(self, paciente, n):
        self._slot(paciente)["alvos"] += n

    def registrar_baixado(self, paciente):
        self._slot(paciente)["baixados"] += 1

    def registrar_metodo(self, metodo):
        # Conta qual estratégia de extração salvou cada PDF (IFRAME, BLOB,
        # HTML->PDF, ...), p/ diagnóstico no relatório final.
        self.metodos_download[metodo] = self.metodos_download.get(metodo, 0) + 1

    def registrar_ignorado(self, paciente):
        self._slot(paciente)["ignorados"] += 1

    def registrar_erro(self, *, paciente, cpf, data_exame, nome_exame, motivo, etapa):
        self._slot(paciente)["falhas"] += 1
        self.erros.append({
            "timestamp": datetime.now().isoformat(),
            "paciente": paciente,
            "cpf": cpf,
            "data_exame": data_exame,
            "nome_exame": nome_exame,
            "etapa": etapa,
            "motivo": motivo,
        })

    def registrar_paciente_processado(self):
        self.pacientes_processados += 1

    def registrar_paciente_nao_encontrado(self, paciente):
        self.pacientes_nao_encontrados += 1
        self._slot(paciente)

    def migrar_pendentes_legados(self):
        if not os.path.exists(PENDENTES_IA_LEGACY_FILE):
            return 0
        try:
            with open(PENDENTES_IA_LEGACY_FILE, 'r', encoding='utf-8') as f:
                pendentes = json.load(f)
        except Exception as e:
            print(f"AVISO: Falha ao ler {PENDENTES_IA_LEGACY_FILE}: {e}")
            return 0

        for item in pendentes:
            self.registrar_erro(
                paciente=item.get("nome", "DESCONHECIDO"),
                cpf=item.get("cpf", ""),
                data_exame=item.get("data_exame", ""),
                nome_exame=item.get("nome_exame", ""),
                motivo="Pendente legado da fase de IA (antes da remoção do agente).",
                etapa="legado_pendente_ia",
            )

        try:
            os.remove(PENDENTES_IA_LEGACY_FILE)
            print(f"    [MIGRAÇÃO] {len(pendentes)} pendente(s) legado(s) migrados e arquivo removido.")
        except Exception as e:
            print(f"AVISO: Não consegui remover {PENDENTES_IA_LEGACY_FILE}: {e}")

        return len(pendentes)

    def _finalizar_timer(self):
        if self._end is None:
            self._end = time.perf_counter()

    def _tempo_formatado(self):
        self._finalizar_timer()
        total = int(self._end - self._start)
        horas, resto = divmod(total, 3600)
        minutos, segundos = divmod(resto, 60)
        if horas:
            return f"{horas}h {minutos}min {segundos}s"
        if minutos:
            return f"{minutos}min {segundos}s"
        return f"{segundos}s"

    def salvar_erros(self):
        path = os.path.join(
            REPORTS_DIR,
            f"erros_{self.timestamp:%d-%m-%Y_%H%M%S}.json",
        )
        payload = {
            "execucao": self.timestamp.strftime("%d-%m-%Y %H:%M:%S"),
            "total_erros": len(self.erros),
            "erros": self.erros,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        return path

    # [MÉTRICAS] Grava os dados brutos da execução (Blocos 1 e 3) em JSON.
    # É puramente aditivo: não altera erros_*.json nem execucao_*.txt.
    def salvar_telemetria(self):
        self._finalizar_timer()
        os.makedirs(TELEMETRIA_DIR, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "inicio": self.timestamp.isoformat(),
            "duracao_s": round(self._end - self._start, 3),
            "login_ok": self.login_ok,
            "pacientes_processados": self.pacientes_processados,
            "pacientes_nao_encontrados": self.pacientes_nao_encontrados,
            "total_erros": len(self.erros),
            "erros": self.erros,  # mesma lista de erros_*.json (p/ "Erros por Tipo" no refino)
            "por_paciente": self.por_paciente,
            "exames": self.exames_eventos,
        }
        path = os.path.join(TELEMETRIA_DIR, f"run_{self.run_id}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        return path

    def gerar_relatorio_final(self):
        self._finalizar_timer()
        total_baixados = sum(s["baixados"] for s in self.por_paciente.values())
        total_falhas = sum(s["falhas"] for s in self.por_paciente.values())
        total_ignorados = sum(s.get("ignorados", 0) for s in self.por_paciente.values())

        linhas = []
        linhas.append("=" * 60)
        linhas.append(
            f"RELATÓRIO FINAL DE EXECUÇÃO — {self.timestamp:%d-%m-%Y %H:%M:%S}"
        )
        linhas.append("=" * 60)
        linhas.append(f"Tempo total: {self._tempo_formatado()}")
        linhas.append("")
        linhas.append(f"Pacientes processados: {self.pacientes_processados}")
        linhas.append(
            f"Pacientes não encontrados no portal: {self.pacientes_nao_encontrados}"
        )
        linhas.append("")
        linhas.append(f"Total de exames baixados com sucesso: {total_baixados}")
        linhas.append(
            f"Total de exames ignorados (localização pré-op / cópias de laudo): {total_ignorados}"
        )
        linhas.append(
            f"Total de exames com falha: {total_falhas} "
            f"(ver erros_*.json para detalhes)"
        )
        if self.metodos_download:
            metodos = ", ".join(
                f"{metodo}: {qtd}"
                for metodo, qtd in sorted(self.metodos_download.items())
            )
            linhas.append(f"Métodos de download usados: {metodos}")
        linhas.append("")
        linhas.append("Quebra por paciente:")
        if not self.por_paciente:
            linhas.append("  (nenhum paciente processado)")
        else:
            for nome, s in self.por_paciente.items():
                linhas.append(
                    f"  {nome} — alvos: {s['alvos']} | "
                    f"baixados: {s['baixados']} | ignorados: {s.get('ignorados', 0)} | "
                    f"falhas: {s['falhas']}"
                )
        linhas.append("=" * 60)

        path = os.path.join(
            REPORTS_DIR,
            f"execucao_{self.timestamp:%d-%m-%Y_%H%M%S}.txt",
        )
        conteudo = "\n".join(linhas) + "\n"
        with open(path, 'w', encoding='utf-8') as f:
            f.write(conteudo)
        return path, conteudo
