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
SEARCH_SCROLL_CICLOS = 3         # ciclos sem novas linhas antes de parar de rolar a lista
STUDY_WAIT_TIMEOUT = 8000        # ms para os cards de exame aparecerem (timeout -> 0 exames)
LAUDO_WAIT_TIMEOUT = 4.0         # teto (s) para o laudo atualizar/estabilizar após clicar o estudo
REPORT_POPUP_TIMEOUT = 5.0       # teto (s) para o iframe do relatório aparecer no popup de print
REPORT_POPUP_RETRIES = 1         # tentativas extras se o popup não abrir (timeout transitório)

# Dedup de cópias: o portal expõe 1 laudo combinado em vários cards (MAMOGRAFIA /
# US MAMARIA / US AXILAR), gerando PDFs com o mesmo conteúdo clínico. Entre as
# cópias mudam a data e os identificadores do rodapé (alguns nem aparecem em
# todas), então a comparação é por SIMILARIDADE do texto, não por hash exato.
DEDUP_SIMILARIDADE = 0.90   # ratio mínimo (difflib) para considerar o mesmo laudo
DEDUP_JANELA_DIAS = 7       # só deduplica cards a <= N dias (cópias ficam em 1-4)
DEDUP_LOG_RATIO_MIN = 0.85  # loga o ratio quando ficar perto mas abaixo do limiar

EXAM_EXCLUDE_KEYWORDS = [
    "LOCALIZACAO PRE",
    "PRE CIRURGICA",
    "PRE-CIRURGICA",
    "PRE OPERATORIA",
    "PRE-OPERATORIA",
]

# O termo autoriza SÓ mamografia e ecografia (mamária/axilas). O nome do card
# casa palavras como MAMA/BREAST/NODULO mesmo quando a modalidade é outra —
# ex.: "MR BREAST RM DE MAMA - PESQUISA NODULO" (ressonância) passava no filtro.
# \b...\b é obrigatório em MR/RM/TC/CT/PET: sem word-boundary, "RM" casaria
# dentro de TERMO/CONFIRMA e barraria exame válido.
EXAM_MODALIDADES_PROIBIDAS = (
    r"\bMR\b|\bRM\b|RESSONANCIA|\bTC\b|\bCT\b|TOMOGRAFIA|"
    r"\bPET\b|CINTILOGRAFIA|CINTILO|DENSITOMETRIA"
)
# Procedimentos (intervenção, não exame diagnóstico).
EXAM_PROCEDIMENTOS_PROIBIDOS = (
    r"BIOPSIA|BIOPSY|PUNCAO|DEMARCA|LOCALIZACAO|LOCALIZATION|NEEDLE"
)

# Cards sem modalidade no nome (ex.: "** SOMENTE RELATÓRIO ** BREAST") passam no
# filtro de card e são decididos pelo LAUDO. A checagem é feita só no CABEÇALHO
# (título do exame), nunca no corpo: um laudo de RM cita "Ecografia mamária" na
# Informação Clínica e um de mamografia pode recomendar RM na conclusão —
# procurar no texto todo erraria nos dois sentidos.
EXAM_REPORT_RM_MARKERS = ["RESSONANCIA MAGNETICA", "RM DE MAMA"]
EXAM_REPORT_HEADER_DELIM = "INFORMACAO CLINICA"  # daqui pra baixo é corpo do laudo
EXAM_REPORT_HEADER_MAX = 400                     # fallback se o delimitador sumir

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
