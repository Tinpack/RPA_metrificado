<!-- [MÉTRICAS] -->
# Telemetria e Métricas do RPA

Captura de métricas para a planilha `metricas_rpa (1).xlsx`, **foco em**:

- **Bloco 1 — Extração:** VP/FP/FN/VN → Acurácia, Precisão, Revocação, F1
- **Bloco 3 — Sucesso por etapa:** Login / Busca / Download unitário / Download completo

> Todo o código de métricas está marcado com o comentário `# [MÉTRICAS]`.
> Blocos 2 (operacional) e 4 (T vs T+7) e a parte de agente de IA/custo **não** estão no escopo.

## Como funciona (2 camadas)

1. **Coleta (junto da automação)** — o `ReportManager` ([../report_manager.py](../report_manager.py)) grava os dados brutos de cada execução em `metricas/telemetria/run_<run_id>.json`. É puramente aditivo: os relatórios `reports/erros_*.json` e `reports/execucao_*.txt` continuam iguais.
2. **Refino (passo separado)** — [../calcular_metricas.py](../calcular_metricas.py) lê os JSONs brutos + o gabarito e gera dois relatórios em `metricas/refinado/`.

## Janela de avaliação (2024–2025)

Só exames com **ano entre 2024 e 2025** entram na conta (`EXAM_YEAR_CUTOFF`/`EXAM_YEAR_MAX` em
[../config.py](../config.py)). Vale tanto para o **RPA** (`check_exam_date` — exames fora da
janela viram `ignorado_data` e não são baixados) quanto para as **métricas** (exames fora da
janela saem como `FORA_JANELA`, fora do universo VP/FP/FN/VN). Isso remove a inflação de VN
por exames antigos e ignora exames de 2026+. O `gabarito.csv` também é regenerado só com a
janela.

## Fluxo de uso

```
1. python main.py                               # roda o RPA -> gera telemetria
2. python calcular_metricas.py --gerar-gabarito # (re)cria o gabarito, SÓ janela 2024-2025
3. (abre metricas/gabarito.csv e corrige a coluna "autorizado")
4. python calcular_metricas.py --validar-gabarito  # confere o gabarito contra o termo (.xlsx)
5. python calcular_metricas.py                  # gera geral.csv + detalhado_<run_id>.csv
```

Repita a cada execução: o gabarito **preserva** suas correções dos exames na janela (passo 2
faz backup em `gabarito.csv.bak` e só emite exames de 2024–2025). O passo 4 é opcional, só
para conferir a conformidade — não altera arquivos.

## Relatórios gerados (`metricas/refinado/`)

- **`geral.csv`** — **uma linha por execução** + linha `Média`. Contém tudo que vai para a planilha `metricas_rpa` (Blocos 1 e 3) + operacional. Reconstruído a cada rodada a partir de toda a telemetria (idempotente).
- **`detalhado_<run_id>.csv`** — "raio-x" de uma execução, seccionado: Resumo Executivo, Métricas por Etapa, Downloads por Paciente (com tempo), Erros por Tipo, e Classificação por Exame (VP/FP/FN/VN linha a linha).

### Dicionário de colunas do `geral.csv`
| Grupo | Colunas |
|---|---|
| Identificação | `Data`, `Hora` |
| Operacional | `Total_Pacientes`, `Exames_Sucesso`, `Total_Erros`, `Taxa_Sucesso_%`, `Tempo_Total_s`, `Tempo_Medio_Paciente_s`, `Tipos_Erro` |
| **Bloco 1** | `VP`, `FP`, `FN`, `VN`, `Total`, `Sem_Gabarito`, `Fora_Janela`, `Duplicados`, `Acuracia`, `Precisao`, `Revocacao`, `F1` |
| **Bloco 3** | `Total_Alvos`, `Login_OK`, `Busca_OK`, `Download_unitario_OK`, `Download_completo_OK`, `Taxa_Login`, `Taxa_Busca`, `Taxa_Download_unitario`, `Taxa_Download_completo` |
| Custo (Fargate) | `Custo_Infra_USD`, `Custo_por_1000_exec` |

📐 **A fórmula de cada coluna está em [CALCULOS.md](CALCULOS.md)** — inclui os denominadores das taxas, o cálculo do custo e como a linha `Média` agrega (soma nas contagens, média nas taxas).

## O gabarito (`metricas/gabarito.csv`)

Como só há gabarito de **pacientes** (não por exame), o `--gerar-gabarito` monta um gabarito **por exame** já pré-preenchido com o palpite do RPA e você só revisa.

| coluna | descrição |
|---|---|
| `paciente`, `cpf` | identificação (cpf é só referência) |
| `data_exame` | data/hora como no card (ex.: `05/06/2025 09:52`) |
| `nome_exame` | nome do exame como no card |
| `decisao_rpa` | **apoio à revisão** (o que o RPA fez); ignorado no cálculo |
| `autorizado` | `1` se o exame **deveria** ser coletado, `0` se não |

Pré-preenchimento: `autorizado=1` para `decisao_rpa ∈ {baixado, ja_no_historico, alvo}`, senão `0`.
**Foque a revisão nos `ignorado_irrelevante`** — é onde o filtro pode ter descartado um exame de mama por engano (vira FN e derruba a Revocação).

O casamento com a telemetria é por `paciente + data_exame + nome_exame` (ignorando acentos/maiúsculas). Exame que estiver na telemetria mas não no gabarito sai como `SEM_GABARITO` (fora do cálculo de VP/FP/FN/VN, contado à parte). Exame com data fora de 2024–2025 sai como `FORA_JANELA` (também fora do cálculo, checado **antes** do gabarito).

## Validação contra o termo de consentimento (`--validar-gabarito`)

`termo_consentimento_exames_aprovados_2024_2025.xlsx` é a verdade-base: matriz onde **cada coluna é um paciente** (linha 1) e **cada célula abaixo é UM exame** — um laudo combinado que apenas *menciona* várias modalidades (mamografia, ecografia mamária, ecografia das axilas) no seu texto — **sem data**. Ex.: um bloco "MAMOGRAFIA + ECO MAMÁRIA + ECO DAS AXILAS" conta como **1 exame**, não 3. `python calcular_metricas.py --validar-gabarito` conta, **por paciente**, o nº de **exames esperados** (termo) vs. o nº de exames com `autorizado=1` no gabarito (na janela), e imprime avisos: **déficit** (possível FN), **excesso** (possível FP), paciente **fora do termo** e **sem cobertura**. É só conferência — **não altera** o gabarito. Leitura do `.xlsx` é via `zipfile` (stdlib, sem `openpyxl`).

> ⚠️ O gabarito é **por card** do portal e um laudo combinado costuma aparecer em vários cards, então o **autorizado** pode superestimar vs. o termo (aparece como `[DIVERGENCIA] excesso`). Isso é esperado até resolver a deduplicação card→laudo no download.

## Convenções e suposições

- **`baixado` / `ja_no_historico`** contam como *coletado* (positivo) no Bloco 1.
- **Decisões possíveis na telemetria:** `baixado`, `ja_no_historico`, `ignorado_data` (fora da janela), `ignorado_irrelevante` (não bate as palavras-alvo), `ignorado_apenas_imagens`, `ignorado_marcador` (biópsia/pré-op puro pelo título do laudo), `ignorado_ressonancia` (RM pelo cabeçalho), `ignorado_duplicado` (cópia do mesmo laudo já baixado). Só `baixado`/`ja_no_historico` contam como coletado.
- O mesmo exame às vezes aparece em vários cards do portal → o **download deduplica** por conteúdo do laudo (`ignorado_duplicado`), e o refino também **deduplica** (mantém a decisão de maior prioridade: `baixado` > `ja_no_historico` > ignorados).
- **Login** é por execução; no Bloco 3 é replicado a cada paciente. Ajustável em `calcular_etapas`.
- Divisões com denominador zero saem vazias (espelha o `IFERROR` da planilha).
