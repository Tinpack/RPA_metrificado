import os
from dotenv import load_dotenv

load_dotenv()

IS_DOCKER = os.path.exists('/.dockerenv') or os.getenv('DOCKER_CONTAINER', 'false').lower() == 'true'
# Headless a menos que explicitamente "false". No Docker/Fargate (sem X server)
# TEM que ser headless — HEADLESS=true (default da imagem) cai aqui em True.
HEADLESS_MODE = os.getenv('HEADLESS', str(IS_DOCKER)).lower() != 'false'

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
# Conta de serviço do Google Sheets. Configurável por env para o deploy AWS, onde
# o arquivo é montado via EFS (não embutido na imagem) — pode ficar fora de /app.
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "credenciais.json")
# Se setado (ex.: s3://bucket/rpa), o RPA sobe telemetria+reports pro S3 ao final
# da execução — pro grupo baixar e rodar as métricas sem acessar o EFS. Vazio =
# não sobe nada (comportamento local normal). Os PDFs de laudo NÃO sobem (LGPD).
RESULTS_S3_URI = os.getenv("RESULTS_S3_URI", "")

# Custo de infraestrutura (ECS Fargate) — base do "Custo por Execução" (Seção 2.6
# do artigo). Os valores de vCPU/memória devem BATER com a task definition; os
# preços são US East (do documento de deploy). Tudo configurável por env para não
# precisar reeditar código ao mudar de tamanho de task ou de região.
INFRA_VCPU = float(os.getenv("INFRA_VCPU", "1.0"))
INFRA_MEM_GB = float(os.getenv("INFRA_MEM_GB", "2.0"))
FARGATE_VCPU_HORA = float(os.getenv("FARGATE_VCPU_HORA", "0.04048"))
FARGATE_MEM_GB_HORA = float(os.getenv("FARGATE_MEM_GB_HORA", "0.004445"))

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
# DEDUP_SIMILARIDADE: com a captura do laudo ESTABILIZADA (_laudo_frame_estavel em
# core.py), a cópia real do mesmo laudo pontua ~0,98 de forma consistente e um stub
# distinto (ex.: US AXILAR "Somente relatório", quase vazio) fica ~0,89 — logo 0,90
# separa os dois com folga. NÃO baixar daqui: abaixo de 0,90 o stub (a manter) colide
# com cópias, e o problema de vazamento era captura instável (resolvido), não o número.
DEDUP_SIMILARIDADE = 0.90   # ratio mínimo (difflib) para considerar o mesmo laudo
DEDUP_JANELA_DIAS = 7       # só deduplica cards a <= N dias (cópias ficam em 1-4)
DEDUP_LOG_RATIO_MIN = 0.70  # loga ratios a partir daqui (near-miss abaixo do limiar) e
                            # serve de corte do quick_ratio. Só afeta LOG, não a decisão.

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
# Procedimentos (intervenção, não exame diagnóstico). "GUIADA/GUIADO POR" é o sinal
# geral de exame guiado por imagem (biópsia/punção/localização) — pega tipos ainda
# não enumerados. Validado: dos nomes reais, só um casa e já era bloqueado por
# PUNCAO/BIOPSIA, então adicioná-lo não barra nenhum exame válido.
EXAM_PROCEDIMENTOS_PROIBIDOS = (
    r"BIOPSIA|BIOPSY|PUNCAO|DEMARCA|LOCALIZACAO|LOCALIZATION|NEEDLE|GUIAD[AO] POR"
)

# Cards sem modalidade no nome (ex.: "** SOMENTE RELATÓRIO ** BREAST") passam no
# filtro de card e são decididos pelo LAUDO. A checagem é feita só no CABEÇALHO
# (título do exame), nunca no corpo: um laudo de RM cita "Ecografia mamária" na
# Informação Clínica e um de mamografia pode recomendar RM na conclusão —
# procurar no texto todo erraria nos dois sentidos.
EXAM_REPORT_RM_MARKERS = ["RESSONANCIA MAGNETICA", "RM DE MAMA"]
EXAM_REPORT_HEADER_DELIM = "INFORMACAO CLINICA"  # daqui pra baixo é corpo do laudo
EXAM_REPORT_HEADER_MAX = 400                     # fallback se o delimitador sumir

# Marcadores no CORPO do laudo que SINALIZAM (não decidem) uma possível carta de
# procedimento. O nome do card às vezes engana (diz "US MAMARIA" mas o laudo é
# "BIOPSIA DE MAMA"), então esses marcadores no corpo disparam uma 2ª checagem.
#
# ATENÇÃO: "PREZADO(A) COLEGA" sozinho NÃO basta para pular — validado no portal
# que laudos diagnósticos VÁLIDOS (ecografia/mamografia) TAMBÉM trazem essa
# saudação (ex.: a ecografia da paciente Janete). Por isso, quando um destes
# marcadores aparece, a decisão final é pelo TÍTULO do laudo
# (`_laudo_tem_titulo_valido` em core.py): mantém se o título for exame
# diagnóstico válido; só pula biópsia/pré-op PUROS (título é o procedimento).
EXAM_REPORT_EXCLUDE_MARKERS = [
    "LOCALIZACAO PRE-OPERATORIA",
    "LOCALIZACAO PRE OPERATORIA",
    "PREZADO(A) COLEGA",
]
