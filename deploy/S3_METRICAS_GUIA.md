# Guia — Retirar métricas do EFS via S3 (RPA e APA)

Como as duas soluções rodam no Fargate e gravam a telemetria no **EFS**
(`fs-01ee9f4ac9ecfb281`, só montável dentro da VPC), este guia mostra o padrão que
o **RPA já usa** e que o **APA deve replicar** para tirar as métricas sem acessar o
EFS: cada solução **sobe a própria telemetria pro S3** ao fim da run; o grupo baixa
do S3 e roda o cálculo de custo local.

## Infra compartilhada (já criada)

| Recurso | Valor |
|---|---|
| Bucket | `s3://clickpalmia-metricas-857145323577` (us-east-1, privado) |
| Prefixo RPA | `rpa/` (telemetria em `rpa/telemetria/`, relatórios em `rpa/reports/`) |
| Prefixo APA | `apa/` (a criar pelo APA) |
| Permissão | inline policy **`metricas-s3-rw`** no role **`clickpalmia-apa-task-role`** (PutObject/GetObject/ListBucket no bucket) — **já cobre RPA e APA**, pois os dois usam esse task role |

> Só sobem **telemetria + relatórios** (JSON/TXT). Os **PDFs de laudo NÃO** vão pro
> S3 (dados de paciente — LGPD); ficam no EFS.

## Como o RPA faz (referência)

- Env `RESULTS_S3_URI=s3://clickpalmia-metricas-857145323577/rpa` na task definition.
- No fim da execução, o código sobe `TELEMETRIA_DIR` e `REPORTS_DIR` pro S3 via
  `boto3` (`main.py::_subir_resultados_s3`). `boto3` está no `requirements.txt`.
  Pega as credenciais automaticamente do **task role** (não precisa configurar nada).

## Como o APA replica (passo a passo)

O APA usa o **mesmo** `clickpalmia-apa-task-role`, então a permissão de S3 **já
existe** — não precisa mexer em IAM. Falta só o APA **subir** a própria telemetria:

1. **Garantir `boto3`** (ou o AWS CLI) na imagem do APA.
2. **Ao fim da run do APA**, subir a telemetria/relatórios para o prefixo `apa/`.
   Exemplo mínimo (Python/boto3), análogo ao do RPA:
   ```python
   import os, glob, boto3
   from urllib.parse import urlparse
   uri = os.getenv("RESULTS_S3_URI", "")   # ex.: s3://clickpalmia-metricas-857145323577/apa
   if uri:
       p = urlparse(uri); bucket, prefixo = p.netloc, p.path.strip("/")
       s3 = boto3.client("s3")
       for base in (TELEMETRIA_DIR_DO_APA, REPORTS_DIR_DO_APA):
           for arq in glob.glob(os.path.join(base, "*")):
               if os.path.isfile(arq):
                   s3.upload_file(arq, bucket, f"{prefixo}/{os.path.basename(base)}/{os.path.basename(arq)}")
   ```
   (Ou, sem tocar no código: uma task `aws-cli` que monta o EFS e faz
   `aws s3 sync <dir do APA no EFS> s3://clickpalmia-metricas-857145323577/apa`.)
3. **Setar a env** `RESULTS_S3_URI=s3://clickpalmia-metricas-857145323577/apa` na
   task definition do APA (prefixo `apa/`, para não misturar com o RPA).

## Como o grupo baixa e roda as métricas

Depois que a(s) task(s) rodou(aram):
```bash
# baixa a telemetria das duas soluções
aws s3 sync s3://clickpalmia-metricas-857145323577/rpa/telemetria ./metricas/telemetria
aws s3 sync s3://clickpalmia-metricas-857145323577/apa/telemetria ./metricas/telemetria   # quando o APA subir

# gera o geral.csv com Custo_Infra_USD / Custo_por_1000_exec
python calcular_metricas.py
```
As colunas de custo saem do bloco `infra` que cada solução grava na telemetria
(`vcpu`, `memoria_gb`, `duracao_s`, preços) — mesma fórmula do documento de deploy.
Como RPA e APA usam **cpu/memória idênticos (1 vCPU / 2 GB)**, a comparação de custo
de infra é justa; o APA soma ainda o custo de **LLM** (bloco `llm` próprio dele).
