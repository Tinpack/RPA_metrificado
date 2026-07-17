# RPA de Extração de Exames (Playwright)

Pipeline de extração de exames do portal HMV, 100% determinístico via **Playwright**. O robô lê a fila de pacientes do Google Sheets, baixa os exames relevantes a partir de 2024 (salvos localmente em `downloads/`) e gera relatórios de execução e de erros para intervenção manual.

---

## Arquitetura do Projeto

Princípios: separação de responsabilidades, **uma única sessão de browser** para todo o lote (não fecha entre pacientes — volta para a aba de busca), e relatórios estruturados em cada execução.

---

## Estrutura de Arquivos

* **`main.py` (Orquestrador):**
  Lê a fila de pacientes do Google Sheets (onde `STATUS = 1`). Abre o navegador e faz login **uma única vez**. Itera os pacientes chamando o `core.py`, resetando a barra de pesquisa entre cada um. No final, fecha o browser e gera os relatórios.
* **`core.py` (RPA):**
  Contém o robô Playwright. Funções principais: `do_login` (idempotente — só preenche o form se o portal mostrar), `buscar_paciente` (pesquisa e aguarda o resultado certo via o contador da aba "Localizar paciente (N)", saindo cedo como não-encontrado quando o contador é `(0)`), `reset_para_busca` (volta para a aba "Localizar paciente" e fecha as guias de paciente abertas — ação interna do app, sem recarregar; re-login só se a sessão cair), `process_patient_exams` (busca o paciente, mapeia exames de 2024+, **ignora laudos de localização pré-operatória / cartas de procedimento**, baixa e salva os PDFs em `downloads/`). Lida com extração de PDFs dentro de `iframes` e conversão de visualizadores HTML em PDF.
* **`report_manager.py` (Relatórios):**
  Classe `ReportManager` acumula estatísticas durante a execução e produz dois arquivos em `reports/` ao final:
  - `erros_DD-MM-YYYY_HHMMSS.json` — lista cada falha com paciente, CPF, data do exame, nome do exame, etapa (`patient_not_found`, `relatorio_indisponivel`, `extract_pdf_failed`, `exam_crash`, `patient_session_crash`) e motivo. Permite intervenção manual.
  - `execucao_DD-MM-YYYY_HHMMSS.txt` — total de pacientes processados, total de exames baixados, total de exames ignorados (localização pré-operatória), quebra por paciente (alvos, baixados, ignorados, falhas) e tempo total de execução.
* **`data_manager.py` (Histórico):**
  Gerencia o `historico_downloads.json` (para não baixar exames duplicados em execuções futuras) e utilitários de normalização de nomes.
* **`config.py` (Configurações Globais):**
  Variáveis de ambiente, caminhos de diretórios, seletores CSS do portal e constantes de filtro/timeout: `EXAM_YEAR_CUTOFF = 2024`, `EXAM_EXCLUDE_KEYWORDS` (nomes de exame a ignorar, ex.: localização pré-cirúrgica), `EXAM_REPORT_EXCLUDE_MARKERS` (marcadores no texto do laudo que indicam carta de procedimento, não exame diagnóstico) e os timeouts de busca/exames (`SEARCH_TIMEOUT`, `SEARCH_NOT_FOUND_GRACE`, `STUDY_WAIT_TIMEOUT`, `LAUDO_WAIT_TIMEOUT`, `REPORT_POPUP_TIMEOUT`).

---

## Fluxo de Execução

1. **Leitura:** O `main.py` consulta o Google Sheets e puxa pacientes com `STATUS = 1`.
2. **Login único:** O Playwright abre o navegador e faz login no portal HMV uma vez.
3. **Processamento:** Para cada paciente:
   - Preenche o nome na barra de pesquisa e aguarda o resultado pelo contador da aba; abre o prontuário.
   - Mapeia exames de **2024 em diante** que casem com as palavras-chave (mamografia, ultrassonografia etc.), que **não** sejam de localização pré-cirúrgica (pelo nome) e que ainda não estejam no histórico de downloads. Duplicatas do mesmo card (mesma data/hora + texto) são colapsadas.
   - Para cada exame: clica no estudo e lê o laudo; se o conteúdo for carta de localização pré-operatória / procedimento, **ignora** (não baixa). Caso contrário, clica em Imprimir, extrai o PDF (via URL direta, blob, ou render HTML→PDF como fallback) e salva em `downloads/`.
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

### Ajustando o corte de ano dos exames
Para mudar o ano a partir do qual os exames são considerados, edite `EXAM_YEAR_CUTOFF` em `config.py`.

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
