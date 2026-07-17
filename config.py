import os
from dotenv import load_dotenv

load_dotenv()

IS_DOCKER = os.path.exists('/.dockerenv') or os.getenv('DOCKER_CONTAINER', 'false').lower() == 'true'
HEADLESS_MODE = os.getenv('HEADLESS', str(IS_DOCKER)).lower() == 'false'

SITE_URL = "https://portalpacientesexames.hmv.org.br/portal/WebLogin.aspx?force_all_browsers=truebr/"
USER = os.getenv("PORTAL_USER", "")
PASS = os.getenv("PORTAL_PASS", "")

USER_FIELD_SELECTOR = "loginUsernameInput"
PASS_FIELD_SELECTOR = "loginPassword"
LOGIN_BUTTON_SELECTOR = "login-button"
SEARCH_BAR_SELECTOR = "sptGeneralDetailsInput"

DOWNLOAD_DIR = os.path.abspath(os.getenv("DOWNLOAD_DIR", "./downloads"))
HISTORY_FILE = os.path.abspath(os.getenv("HISTORY_FILE", "./historico_downloads.json"))
REPORTS_DIR = os.path.abspath(os.getenv("REPORTS_DIR", "./reports"))

EXAM_YEAR_CUTOFF = 2024
EXAM_YEAR_MAX = 2025

# Timeouts da busca de paciente / espera de exames
SEARCH_TIMEOUT = 10.0            # teto (s) para localizar o paciente na tabela
SEARCH_NOT_FOUND_GRACE = 2.0     # tempo (s) antes de confiar no badge "(0)" = sem resultado
STUDY_WAIT_TIMEOUT = 8000        # ms para os cards de exame aparecerem (timeout -> 0 exames)
LAUDO_WAIT_TIMEOUT = 4.0         # teto (s) para o laudo atualizar/estabilizar após clicar o estudo
REPORT_POPUP_TIMEOUT = 5.0       # teto (s) para o iframe do relatório aparecer no popup de print

EXAM_EXCLUDE_KEYWORDS = [
    "LOCALIZACAO PRE",
    "PRE CIRURGICA",
    "PRE-CIRURGICA",
    "PRE OPERATORIA",
    "PRE-OPERATORIA",
]

# Marcadores no texto do laudo que indicam que NÃO é um exame diagnóstico e
# não deve ser enviado para a API. O nome do card às vezes engana (diz "MAMO"
# mas o laudo é outra coisa), então a checagem é feita no texto do laudo.
#
# "PREZADO(A) COLEGA": validado no portal — laudos com essa saudação são
# cartas de encaminhamento/procedimento (localização pré-op, demarcação,
# biópsia), nunca o laudo diagnóstico em si. Ex.: card "US MAMARIA" cujo
# Procedimento real era "BIOPSIA DE MAMA". Os laudos diagnósticos reais
# (mamografia, US) NÃO trazem essa saudação. NÃO remover sem revalidar.
EXAM_REPORT_EXCLUDE_MARKERS = [
    "LOCALIZACAO PRE-OPERATORIA",
    "LOCALIZACAO PRE OPERATORIA",
    "PREZADO(A) COLEGA",
]
