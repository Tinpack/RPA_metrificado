# Deploy do RPA na AWS (ECS Fargate) + métricas de custo

Guia objetivo para subir o RPA (e, análogo, o APA) como **task one-shot** no ECS
Fargate e coletar o **custo por execução** compatível com a Seção 2.6 do artigo.
Destilado de `Deploy_AWS_Metricas_Custo_RPA_vs_APA.docx`.

> **Comparação justa:** RPA e APA devem rodar com o **mesmo vCPU/memória**
> (aqui: 1 vCPU / 2 GB → `cpu 1024`, `memory 2048`). Se um rodar maior que o outro,
> o custo de infra já vem viesado.

---

## 0. Pré-requisitos (uma vez, na máquina)

1. **Docker Desktop** e **AWS CLI v2** instalados (e o Docker Desktop **aberto** —
   o daemon precisa estar rodando para `build`/`push`).
2. **Credenciais de API** (Access Key). O CSV de *login do console*
   (usuário/senha/URL) **não serve** para o CLI. Para obter a access key:
   entre no console (URL/usuário/senha do CSV) → canto sup. direito → **Security
   credentials** → **Create access key** → **CLI** → baixe o `.csv`.
3. Configurar o CLI com esse `.csv` (novo formato, sem coluna "User Name"):
   ```powershell
   # importa sem digitar/expor as chaves; ajuste o caminho do .csv
   $c = Import-Csv "CAMINHO\accessKeys.csv"
   aws configure set aws_access_key_id     $c.'Access key ID'
   aws configure set aws_secret_access_key $c.'Secret access key'
   aws configure set region us-east-1      # casa com o preço US East usado no custo
   aws sts get-caller-identity             # confirma
   ```

---

## 1. Publicar a imagem no ECR

```bash
aws ecr create-repository --repository-name rpa-exames
aws ecr get-login-password | docker login --username AWS \
  --password-stdin <conta>.dkr.ecr.<regiao>.amazonaws.com
docker build -t rpa-exames .
docker tag rpa-exames:latest <conta>.dkr.ecr.<regiao>.amazonaws.com/rpa-exames:latest
docker push <conta>.dkr.ecr.<regiao>.amazonaws.com/rpa-exames:latest
```

## 2. Segredos e persistência

- **Secrets Manager / SSM** para `PORTAL_USER`, `PORTAL_PASS`, `SHEET_URL`
  (referenciados na task definition via `secrets`). Nunca embutir na imagem — o
  `.dockerignore` já exclui `.env`/`credenciais.json`.
- **EFS** montado em `/data` para persistir `downloads/`, `historico_downloads.json`,
  `reports/` e **`telemetria/`** (base do custo) entre execuções. Colocar o
  `credenciais.json` do Google no EFS (ex.: `/data/credenciais.json`) — a task já
  aponta `GOOGLE_CREDS_FILE=/data/credenciais.json`. Sem EFS, cada task começa do
  zero e perde o histórico.

## 3. Cluster + Task Definition

```bash
aws ecs create-cluster --cluster-name comparacao-rpa-apa
# preencha os REPLACE_* em deploy/task-definition-rpa.json e registre:
aws ecs register-task-definition --cli-input-json file://deploy/task-definition-rpa.json
```
Repita para o APA (`apa-exames`) com **cpu/memory idênticos**.

## 4. Disparar a execução

Manual (testes controlados T e T+7):
```bash
aws ecs run-task --cluster comparacao-rpa-apa \
  --task-definition rpa-exames --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<subnet>],securityGroups=[<sg>],assignPublicIp=ENABLED}"
```
Ou agendado via **EventBridge Scheduler** (útil para padronizar as execuções da
planilha de métricas).

---

## 5. Custo por execução (já sai pronto)

O código deste repo já grava os insumos na telemetria e calcula o custo:

- `report_manager.py` grava `payload["infra"]` (vCPU, memória, duração, preços) em
  `metricas/telemetria/run_*.json`.
- `calcular_metricas.py` calcula e joga no `metricas/refinado/geral.csv`:
  **`Custo_Infra_USD`** e **`Custo_por_1000_exec`**. Fórmula:
  ```
  billed_s = max(60, duracao_s)   # Fargate cobra no mínimo 1 min
  Custo = (vCPU × preço_vCPU_hora + memória_GB × preço_mem_hora) × billed_s/3600
  ```
- Preços atuais (US East, do documento): vCPU **US$ 0,04048/h**, memória
  **US$ 0,004445/GB-h**. Ajustáveis por env (`FARGATE_VCPU_HORA`,
  `FARGATE_MEM_GB_HORA`, `INFRA_VCPU`, `INFRA_MEM_GB`) sem tocar no código.

**Auditoria agregada (opcional):** marque as tasks com **Cost Allocation Tag**
`Solucao=RPA` / `Solucao=APA` e confira no **Cost Explorer** filtrando por essa tag.
Atenção: o Cost Explorer tem defasagem de ~24h — só serve para auditar o total do
mês, não para o número por execução (esse já vem do `geral.csv`).

---

## 6. Fora do escopo deste repo (RPA)

- **Custo de LLM** (tokens de entrada/saída × preço do modelo) é do **APA** — lá o
  `report_manager` acrescenta um `payload["llm"]` análogo ao `infra` daqui. Se o APA
  usar **Bedrock (Claude)**, o custo entra no billing AWS (métricas
  `InputTokenCount`/`OutputTokenCount` no CloudWatch ou o campo `usage` da resposta);
  se usar **Gemini**, é cobrança separada da Google (campo `usageMetadata`).

## 7. Pendências a confirmar com o grupo

- Qual LLM no APA (Gemini vs Claude/Bedrock)?
- A conta já tem VPC/subnets/security-group e permissões ECS/ECR/EFS/Secrets para o
  seu usuário IAM, ou precisa provisionar?
