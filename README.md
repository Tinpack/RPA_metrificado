# RPA de Extração de Exames (Playwright)

Pipeline de extração de exames do portal HMV, 100% determinístico via **Playwright**. O robô lê a fila de pacientes do Google Sheets, baixa os exames relevantes da **janela 2024–2025** (mamografia e ecografia de mama/axila), filtrando por modalidade e pelo conteúdo do laudo, deduplicando cópias do mesmo laudo, e salva os PDFs em `downloads/`. Gera relatórios de execução e de erros para intervenção manual.

---

## Arquitetura do Projeto

Princípios: separação de responsabilidades, **uma única sessão de browser** para todo o lote (não fecha entre pacientes — volta para a aba de busca), e relatórios estruturados em cada execução.

---

## Estrutura de Arquivos

* **`main.py` (Orquestrador):**
  Lê a fila de pacientes do Google Sheets (onde `STATUS = 1`). Força **UTF-8 no stdout** (laudos com BOM/acentos derrubavam os prints no console Windows) e abre o navegador com **viewport alto (1280×2200)** — a lista de resultados da busca renderiza só as linhas que cabem na janela, e com 720px o robô perdia pacientes no fim da lista. Faz login **uma única vez**, itera os pacientes chamando o `core.py` e reseta a barra de pesquisa entre cada um. No final, fecha o browser e gera os relatórios.
* **`core.py` (RPA):**
  Contém o robô Playwright. Funções principais: `do_login` (idempotente — só preenche o form se o portal mostrar), `buscar_paciente` (pesquisa e aguarda o resultado certo, match exato do nome; sai cedo como não-encontrado quando o contador da aba é `(0)`), `reset_para_busca` (volta para a aba "Localizar paciente" e fecha as guias de paciente abertas — ação interna do app, sem recarregar; re-login só se a sessão cair), `is_relevant_exam` (aceita/rejeita um exame pelo **nome do card**), `process_patient_exams` (busca o paciente e processa os exames). O processamento aplica, em camadas:
  1. **Filtro por modalidade no card** — aceita só mama/eco e **rejeita** modalidades não autorizadas (ressonância, tomografia, cintilografia, densitometria) e procedimentos (biópsia, punção, localização, *guiada por imagem*);
  2. **Decisão pelo laudo** — se o laudo traz "PREZADO(A) COLEGA"/pré-op no corpo, decide pelo **TÍTULO**: mantém se for exame diagnóstico válido (ecografia/mamografia), ignora se for biópsia/pré-op puro; ressonância é barrada pelo cabeçalho do laudo;
  3. **Dedup por conteúdo** — 1 laudo combinado aparece em vários cards; baixa uma vez só, comparando o texto do laudo por similaridade dentro de uma janela de dias.
  Baixa e salva os PDFs em `downloads/`, lidando com extração dentro de `iframes` e conversão de visualizadores HTML em PDF.
* **`report_manager.py` (Relatórios):**
  Classe `ReportManager` acumula estatísticas durante a execução e produz dois arquivos em `reports/` ao final:
  - `erros_DD-MM-YYYY_HHMMSS.json` — lista cada falha com paciente, CPF, data do exame, nome do exame, etapa (`patient_not_found`, `relatorio_indisponivel`, `extract_pdf_failed`, `exam_crash`, `patient_session_crash`) e motivo. Permite intervenção manual.
  - `execucao_DD-MM-YYYY_HHMMSS.txt` — total de pacientes processados, total de exames baixados, total de exames ignorados (localização pré-op / cópias de laudo), quebra por paciente (alvos, baixados, ignorados, falhas) e tempo total de execução.
  - `metricas/telemetria/run_*.json` — dados brutos por execução, com a **decisão** de cada exame visto: `baixado`, `ja_no_historico`, `ignorado_data`, `ignorado_irrelevante`, `ignorado_apenas_imagens`, `ignorado_marcador` (biópsia/pré-op puro), `ignorado_ressonancia`, `ignorado_duplicado` (cópia do mesmo laudo).
* **`data_manager.py` (Histórico):**
  Gerencia o `historico_downloads.json` (para não baixar exames duplicados em execuções futuras) e utilitários de normalização de nomes.
* **`config.py` (Configurações Globais):**
  Variáveis de ambiente, caminhos de diretórios, seletores CSS do portal e constantes de filtro/timeout. Principais:
  - **Janela de exames:** `EXAM_YEAR_CUTOFF = 2024` e `EXAM_YEAR_MAX = 2025`.
  - **Filtro do card:** `EXAM_MODALIDADES_PROIBIDAS` (regex de modalidades não autorizadas — RM/TC/PET/cintilografia/densitometria), `EXAM_PROCEDIMENTOS_PROIBIDOS` (biópsia/punção/localização/needle/*guiada por*), `EXAM_EXCLUDE_KEYWORDS` (localização pré-cirúrgica pelo nome).
  - **Filtro do laudo:** `EXAM_REPORT_EXCLUDE_MARKERS` (marcadores de carta/procedimento no corpo, ex.: "PREZADO(A) COLEGA") e `EXAM_REPORT_RM_MARKERS` + `EXAM_REPORT_HEADER_DELIM` (detecção de ressonância só no cabeçalho do laudo).
  - **Dedup de cópias:** `DEDUP_SIMILARIDADE` (ratio mínimo de similaridade do laudo), `DEDUP_JANELA_DIAS` (janela de dias entre cards).
  - **Timeouts/busca:** `SEARCH_TIMEOUT`, `SEARCH_NOT_FOUND_GRACE`, `SEARCH_SCROLL_CICLOS`, `STUDY_WAIT_TIMEOUT`, `LAUDO_WAIT_TIMEOUT`, `REPORT_POPUP_TIMEOUT`, `REPORT_POPUP_RETRIES`.

---

## Fluxo de Execução

1. **Leitura:** O `main.py` consulta o Google Sheets e puxa pacientes com `STATUS = 1`.
2. **Login único:** O Playwright abre o navegador e faz login no portal HMV uma vez.
3. **Processamento:** Para cada paciente:
   - Preenche o nome na barra de pesquisa e aguarda o resultado pelo contador da aba; abre o prontuário.
   - Mapeia exames da **janela 2024–2025** que casem com as palavras-chave de mama/eco, **rejeitando pelo nome do card** modalidades não autorizadas (ressonância, tomografia, cintilografia, densitometria) e procedimentos (biópsia, punção, localização, *guiada por imagem*), e que ainda não estejam no histórico de downloads. Duplicatas do mesmo card (mesma data/hora + texto) são colapsadas.
   - Para cada exame: clica no estudo e lê o laudo. Se o laudo tiver marcador de carta/procedimento ("PREZADO(A) COLEGA", pré-op) no corpo, decide pelo **TÍTULO** — mantém se for exame diagnóstico válido (a ecografia/mamografia real também usa essa saudação), ignora se o título for biópsia/pré-op puro; ressonância é barrada pelo cabeçalho. Antes de salvar, compara o texto do laudo com os já baixados do paciente e **pula cópias** (mesmo laudo combinado exposto em vários cards). Caso siga, clica em Imprimir, extrai o PDF (via URL direta, blob, ou render HTML→PDF como fallback) e salva em `downloads/`.
   - Falhas em qualquer etapa são registradas no `ReportManager` com etapa e motivo, e o robô segue para o próximo exame.
4. **Reset entre pacientes:** O robô volta para a aba "Localizar paciente" e fecha a guia do paciente (ação interna do app, sem recarregar nem re-logar); re-login só se a sessão cair. O browser **nunca é fechado** entre pacientes.
5. **Sheets:** Pacientes processados sem falhas — ou que **não foram encontrados** no portal — têm `STATUS` atualizado para `0` (saem da fila). `partial_fail` e `crash` permanecem com `STATUS = 1` para reprocessamento.
6. **Relatórios:** Ao final, `reports/erros_*.json` e `reports/execucao_*.txt` são gerados.

---

## Como Configurar e Executar

### Pré-requisitos
* Python 3.10 ou superior.
* `pip install -r requirements.txt` (dependências: `playwright`, `pandas`, `gspread`, `requests`, `python-dotenv`).
* Instale os navegadores do Playwright: `playwright install`

### Arquivos Sensíveis (não versionados)
Crie na raiz do projeto:

1. **`credenciais.json`**: Chave da Conta de Serviço do Google Cloud para acessar o Google Sheets. O `client_email` precisa ter permissão de **Editor** na planilha.
2. **`.env`**: Variáveis de ambiente:
   ```env
   # Portal de Exames HMV
   PORTAL_USER=seu_usuario
   PORTAL_PASS=sua_senha

   # Google Sheets (planilha com a fila de pacientes)
   SHEET_URL=https://docs.google.com/spreadsheets/d/SEU_ID/edit

   # Execução (true = sem janela visível)
   HEADLESS=true
   ```

### Executar
```
python main.py
```

Os PDFs baixados ficam em `downloads/` e os relatórios em `reports/`.

### Ajustando a janela de anos dos exames
Os exames considerados ficam entre `EXAM_YEAR_CUTOFF` (2024) e `EXAM_YEAR_MAX` (2025) em `config.py` — edite ambos para mudar a janela.

---

## Execução em Docker

A imagem usa `mcr.microsoft.com/playwright/python:v1.60.0-jammy` como base (já vem com Chromium + libs do sistema). O container roda o `main.py` uma vez e sai — ideal para disparar via cron do host, Kubernetes CronJob, ou manualmente.

### Estrutura de dados em runtime

Dentro do container, os caminhos de dados são sobrescritos por variáveis de ambiente:

| Variável | Caminho no container |
|---|---|
| `DOWNLOAD_DIR` | `/data/downloads` |
| `HISTORY_FILE` | `/data/historico_downloads.json` |
| `REPORTS_DIR` | `/data/reports` |

Monte uma pasta `./data` do host em `/data` no container para persistir PDFs baixados, histórico de downloads (anti-duplicação) e relatórios.

`credenciais.json` e `.env` ficam **fora** da imagem — são montados em runtime como secrets.

### Build e execução com docker-compose

```bash
# 1. Garanta que .env e credenciais.json existem na raiz do projeto.
# 2. Crie a pasta de dados persistente:
mkdir -p data

# 3. Build (uma vez):
docker compose build

# 4. Executar (one-shot, sai quando termina):
docker compose run --rm rpa
```

PDFs ficam em `./data/downloads/`, relatórios em `./data/reports/`, histórico em `./data/historico_downloads.json`.

### Build e execução com docker puro

```bash
docker build -t rpa-exames:latest .

docker run --rm \
  --env-file .env \
  -v "$(pwd)/credenciais.json:/app/credenciais.json:ro" \
  -v "$(pwd)/data:/data" \
  rpa-exames:latest
```

### Agendamento em produção

Como o container é one-shot, agende o trigger externamente:

- **Cron do host:** `0 8 * * * cd /opt/rpa-exames && docker compose run --rm rpa >> /var/log/rpa.log 2>&1`
- **Kubernetes:** use `CronJob` apontando para a imagem buildada e publicada num registry.
- **Manualmente:** rode `docker compose run --rm rpa` quando precisar processar a fila.
