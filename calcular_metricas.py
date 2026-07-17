# [MÉTRICAS] Refino das métricas do RPA (Camada 2).
#
# Lê os dados brutos gravados pela automação (metricas/telemetria/run_*.json)
# e o gabarito de autorização (metricas/gabarito.csv) e produz dois relatórios,
# no estilo "geral histórico + detalhado executivo":
#
#   metricas/refinado/geral.csv             -> 1 linha por execução + Média.
#       Contém tudo que vai para a planilha metricas_rpa:
#       operacional (tempo, taxa de sucesso, erros) + Bloco 1 (VP/FP/FN/VN ->
#       Acurácia/Precisão/Revocação/F1) + Bloco 3 (etapas -> taxas).
#   metricas/refinado/detalhado_<run_id>.csv -> um "raio-x" por execução,
#       seccionado (resumo, etapas, downloads por paciente, erros, classificação
#       por exame).
#
# Blocos em foco: 1 (Extração) e 3 (Etapas). Sem dependência do portal.
#
# Uso:
#   python calcular_metricas.py                 # gera os relatórios
#   python calcular_metricas.py --gerar-gabarito # cria/atualiza gabarito pré-preenchido
#   [--telemetria DIR] [--gabarito CSV] [--saida DIR]

import os
import csv
import re
import sys
import json
import glob
import zipfile
import argparse
import unicodedata
from collections import Counter

# Janela de avaliação (mesma fonte usada pelo RPA em config.py): só exames com
# ano em [EXAM_YEAR_CUTOFF, EXAM_YEAR_MAX] entram no VP/FP/FN/VN. Importa de
# config para não duplicar o valor; config não depende do portal.
from config import EXAM_YEAR_CUTOFF, EXAM_YEAR_MAX

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TELEMETRIA_DIR = os.path.join(BASE_DIR, "metricas", "telemetria")
GABARITO_CSV = os.path.join(BASE_DIR, "metricas", "gabarito.csv")
SAIDA_DIR = os.path.join(BASE_DIR, "metricas", "refinado")
TERMO_XLSX = os.path.join(BASE_DIR, "metricas",
                          "termo_consentimento_exames_aprovados_2024_2025.xlsx")

# Decisões que significam "o exame FOI coletado pelo sistema".
# ja_no_historico = autorizado e já baixado numa execução anterior (o PDF está
# no sistema), então conta como coletado mesmo sem ter sido rebaixado agora.
DECISOES_COLETADO = {"baixado", "ja_no_historico"}
# Palpite de "deveria coletar" para pré-preencher o gabarito.
DECISOES_PALPITE_SIM = {"baixado", "ja_no_historico", "alvo"}
# Prioridade ao deduplicar o mesmo exame visto em 2 cards do portal.
PRIORIDADE_DECISAO = {"baixado": 3, "ja_no_historico": 2, "alvo": 1}


# ------------------------------------------------------------- utilidades
def _norm(s):
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(sem_acento.split()).strip().upper()


def _chave(paciente, data_exame, nome_exame):
    return (_norm(paciente), _norm(data_exame), _norm(nome_exame))


def _ano_exame(data_exame):
    # Espelha o parse de core.check_exam_date: "DD/MM/AAAA HH:MM ..." -> AAAA.
    # "Desconhecido" ou formato inesperado -> None (fica fora da janela).
    try:
        return int(str(data_exame).split(" ")[0].split("/")[2])
    except (ValueError, IndexError, AttributeError):
        return None


def _dentro_janela(data_exame):
    ano = _ano_exame(data_exame)
    return ano is not None and EXAM_YEAR_CUTOFF <= ano <= EXAM_YEAR_MAX


# Modalidades de interesse (mama). Usadas para casar o termo (texto livre) com o
# nome_exame do gabarito, contando por categoria. Mesma família de palavras-chave
# de core.is_relevant_exam. Retorna um CONJUNTO: uma linha do termo pode listar
# exame combinado (ex.: "ECOGRAFIA MAMARIA E AXILAR" -> {ECO_MAMA, AXILA}).
CATEGORIAS = ("MAMOGRAFIA", "ECO_MAMA", "AXILA")


def _categorias(texto):
    t = _norm(texto)  # sem acento, maiúsculo
    cats = set()
    if any(k in t for k in ("AXILA", "AXILAR")):
        cats.add("AXILA")
    if any(k in t for k in ("MAMOGRAFIA", "MAMOGRAF", "MAMO", "MMG", "MAMMO")):
        cats.add("MAMOGRAFIA")
    if (any(k in t for k in ("ECOGRAFIA", "ECOGRAF", "US ", " US", "ULTRASSON"))
            and any(k in t for k in ("MAMA", "MAMARIA", "BREAST"))):
        cats.add("ECO_MAMA")
    return cats


def _div(num, den):
    return (num / den) if den else None


def _autorizado_para_bool(valor):
    v = _norm(valor)
    if v in {"1", "SIM", "S", "TRUE", "VERDADEIRO", "AUTORIZADO"}:
        return True
    if v in {"0", "NAO", "N", "FALSE", "FALSO", "NEGADO", ""}:
        return False
    return None


def carregar_runs(telemetria_dir):
    runs = []
    for path in sorted(glob.glob(os.path.join(telemetria_dir, "run_*.json"))):
        with open(path, "r", encoding="utf-8") as f:
            runs.append(json.load(f))
    return runs


def dedup_exames(exames):
    """Colapsa o mesmo exame visto em múltiplos cards numa única entrada,
    mantendo a decisão de maior prioridade (baixado > ja_no_historico > ...)."""
    melhor = {}
    for ev in exames:
        chave = _chave(ev.get("paciente"), ev.get("data_exame"), ev.get("nome_exame"))
        atual = melhor.get(chave)
        if atual is None or (PRIORIDADE_DECISAO.get(ev.get("decisao"), 0)
                             > PRIORIDADE_DECISAO.get(atual.get("decisao"), 0)):
            melhor[chave] = ev
    return list(melhor.values())


def carregar_gabarito(path):
    if not os.path.exists(path):
        return {}
    gabarito = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for linha in csv.DictReader(f):
            campos = {_norm(k): v for k, v in linha.items()}
            chave = _chave(campos.get("PACIENTE", ""),
                           campos.get("DATA_EXAME", ""),
                           campos.get("NOME_EXAME", ""))
            gabarito[chave] = _autorizado_para_bool(campos.get("AUTORIZADO", ""))
    return gabarito


# ---------------------------------------------------------------- Bloco 1
def classificar_exames(exames, gabarito):
    detalhe = []
    vp = fp = fn = vn = sem_gabarito = fora_janela = 0
    for ev in exames:
        # Janela primeiro: exames fora de 2024-2025 não entram no universo
        # VP/FP/FN/VN (evita a inflação de VN por exames antigos e ignora 2026+).
        if not _dentro_janela(ev.get("data_exame")):
            detalhe.append({**ev, "autorizado": None, "classificacao": "FORA_JANELA"})
            fora_janela += 1
            continue

        coletado = ev.get("decisao") in DECISOES_COLETADO
        autorizado = gabarito.get(_chave(ev.get("paciente"),
                                         ev.get("data_exame"),
                                         ev.get("nome_exame")))
        if autorizado is None:
            classe = "SEM_GABARITO"; sem_gabarito += 1
        elif coletado and autorizado:
            classe = "VP"; vp += 1
        elif coletado and not autorizado:
            classe = "FP"; fp += 1
        elif not coletado and autorizado:
            classe = "FN"; fn += 1
        else:
            classe = "VN"; vn += 1
        detalhe.append({**ev, "autorizado": autorizado, "classificacao": classe})

    total = vp + fp + fn + vn
    precisao = _div(vp, vp + fp)
    revocacao = _div(vp, vp + fn)
    f1 = (_div(2 * precisao * revocacao, precisao + revocacao)
          if precisao and revocacao else None)
    resumo = {
        "VP": vp, "FP": fp, "FN": fn, "VN": vn, "Total": total,
        "Sem_Gabarito": sem_gabarito, "Fora_Janela": fora_janela,
        "Acuracia": _div(vp + vn, total), "Precisao": precisao,
        "Revocacao": revocacao, "F1": f1,
    }
    return resumo, detalhe


# ---------------------------------------------------------------- Bloco 3
def calcular_etapas(run):
    por_paciente = run.get("por_paciente", {})
    total = len(por_paciente)
    alvos = sum(s.get("alvos", 0) for s in por_paciente.values())
    login_ok = total if run.get("login_ok") else 0  # login por-run replicado
    busca_ok = sum(1 for s in por_paciente.values() if s.get("busca_ok"))
    dl_unit = sum(s.get("baixados", 0) for s in por_paciente.values())
    dl_completo = sum(1 for s in por_paciente.values() if s.get("download_completo_ok"))
    return {
        "Total_Exec": total, "Total_Alvos": alvos,
        "Login_OK": login_ok, "Busca_OK": busca_ok,
        "Download_unitario_OK": dl_unit, "Download_completo_OK": dl_completo,
        "Taxa_Login": _div(login_ok, total), "Taxa_Busca": _div(busca_ok, total),
        # Download unitário: fração dos exames-alvo baixados (denominador = alvos).
        "Taxa_Download_unitario": _div(dl_unit, alvos),
        "Taxa_Download_completo": _div(dl_completo, total),
    }


# ---------------------------------------------------------- operacional
def calcular_operacional(run):
    por_paciente = run.get("por_paciente", {})
    total_pac = len(por_paciente)
    exames_sucesso = sum(s.get("baixados", 0) for s in por_paciente.values())
    erros = run.get("erros", [])
    total_erros = run.get("total_erros", len(erros))
    tipos = Counter(e.get("etapa", "?") for e in erros)
    tempo_total = run.get("duracao_s")
    inicio = run.get("inicio", "")
    data, _, hora = inicio.partition("T")
    return {
        "Data": data, "Hora": hora[:8],
        "Total_Pacientes": total_pac,
        "Exames_Sucesso": exames_sucesso,
        "Total_Erros": total_erros,
        "Taxa_Sucesso_%": _div(exames_sucesso, exames_sucesso + total_erros),
        "Tempo_Total_s": tempo_total,
        "Tempo_Medio_Paciente_s": _div(tempo_total, total_pac) if tempo_total else None,
        "Tipos_Erro": ";".join(f"{t}:{n}" for t, n in sorted(tipos.items())) or "0",
    }, tipos


# ---------------------------------------------------------------- saída
COLUNAS_GERAL = [
    "Data", "Hora",
    "Total_Pacientes", "Exames_Sucesso", "Total_Erros", "Taxa_Sucesso_%",
    "Tempo_Total_s", "Tempo_Medio_Paciente_s", "Tipos_Erro",
    # Bloco 1
    "VP", "FP", "FN", "VN", "Total", "Sem_Gabarito", "Fora_Janela",
    "Acuracia", "Precisao", "Revocacao", "F1",
    # Bloco 3
    "Total_Alvos", "Login_OK", "Busca_OK", "Download_unitario_OK", "Download_completo_OK",
    "Taxa_Login", "Taxa_Busca", "Taxa_Download_unitario", "Taxa_Download_completo",
]
CONTAGENS = ["Total_Pacientes", "Exames_Sucesso", "Total_Erros", "VP", "FP", "FN",
             "VN", "Total", "Sem_Gabarito", "Fora_Janela", "Total_Alvos", "Login_OK",
             "Busca_OK", "Download_unitario_OK", "Download_completo_OK"]
TAXAS = ["Taxa_Sucesso_%", "Acuracia", "Precisao", "Revocacao", "F1", "Taxa_Login",
         "Taxa_Busca", "Taxa_Download_unitario", "Taxa_Download_completo",
         "Tempo_Total_s", "Tempo_Medio_Paciente_s"]


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return round(v, 4)
    return v


def processar_run(run, gabarito):
    exames = dedup_exames(run.get("exames", []))
    resumo1, detalhe = classificar_exames(exames, gabarito)
    resumo3 = calcular_etapas(run)
    oper, tipos_erro = calcular_operacional(run)
    linha = {"run_id": run.get("run_id", "")}
    linha.update(oper); linha.update(resumo1); linha.update(resumo3)
    return linha, detalhe, tipos_erro


def linha_media(linhas):
    media = {"Data": "Média", "Hora": ""}
    for c in CONTAGENS:
        media[c] = sum(l.get(c, 0) or 0 for l in linhas)
    for t in TAXAS:
        vals = [l[t] for l in linhas if l.get(t) is not None]
        media[t] = (sum(vals) / len(vals)) if vals else None
    media["Tipos_Erro"] = ""
    return media


def escrever_geral(linhas, saida_dir):
    os.makedirs(saida_dir, exist_ok=True)
    path = os.path.join(saida_dir, "geral.csv")
    todas = sorted(linhas, key=lambda l: l["run_id"]) + [linha_media(linhas)]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUNAS_GERAL, extrasaction="ignore")
        w.writeheader()
        for l in todas:
            w.writerow({c: _fmt(l.get(c)) for c in COLUNAS_GERAL})
    return path


def escrever_detalhado(run, linha, detalhe, tipos_erro, saida_dir):
    os.makedirs(saida_dir, exist_ok=True)
    run_id = run.get("run_id", "run")
    path = os.path.join(saida_dir, f"detalhado_{run_id}.csv")

    def taxa(v):
        return "" if v is None else f"{round(v * 100, 2)}%"

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["RELATÓRIO DE EXECUÇÃO - RPA"])
        w.writerow(["Data/Hora", run.get("inicio", "")])
        w.writerow(["Run ID", run_id])
        w.writerow([])

        w.writerow(["=== RESUMO EXECUTIVO ==="])
        w.writerow(["Métrica", "Valor"])
        w.writerow(["Total de Pacientes", linha["Total_Pacientes"]])
        w.writerow(["Exames com Sucesso", linha["Exames_Sucesso"]])
        w.writerow(["Total de Erros", linha["Total_Erros"]])
        w.writerow(["Taxa de Sucesso", taxa(linha["Taxa_Sucesso_%"])])
        w.writerow(["Tempo Total (s)", linha["Tempo_Total_s"]])
        w.writerow(["Tempo Médio/Paciente (s)", _fmt(linha["Tempo_Medio_Paciente_s"])])
        w.writerow(["Login OK", "sim" if run.get("login_ok") else "não"])
        w.writerow(["-- Bloco 1 (Extração) --"])
        for k in ["VP", "FP", "FN", "VN", "Sem_Gabarito", "Fora_Janela"]:
            w.writerow([k, linha[k]])
        for k in ["Acuracia", "Precisao", "Revocacao", "F1"]:
            w.writerow([k, _fmt(linha[k])])
        w.writerow([])

        w.writerow(["=== MÉTRICAS POR ETAPA (Bloco 3) ==="])
        w.writerow(["Etapa", "OK", "Total", "Taxa"])
        tot = linha["Total_Exec"]
        w.writerow(["Login", linha["Login_OK"], tot, taxa(linha["Taxa_Login"])])
        w.writerow(["Busca", linha["Busca_OK"], tot, taxa(linha["Taxa_Busca"])])
        w.writerow(["Download unitário", linha["Download_unitario_OK"], linha["Total_Alvos"],
                    taxa(linha["Taxa_Download_unitario"])])
        w.writerow(["Download completo", linha["Download_completo_OK"], tot,
                    taxa(linha["Taxa_Download_completo"])])
        w.writerow([])

        w.writerow(["=== DOWNLOADS POR PACIENTE ==="])
        w.writerow(["Paciente", "Exames", "Tempo (s)", "Busca OK", "Download Completo"])
        for nome, s in run.get("por_paciente", {}).items():
            w.writerow([nome, s.get("baixados", 0), s.get("tempo_s", ""),
                        _sim_nao(s.get("busca_ok")), _sim_nao(s.get("download_completo_ok"))])
        w.writerow([])

        w.writerow(["=== ERROS POR TIPO ==="])
        w.writerow(["Tipo", "Quantidade"])
        for tipo, n in sorted(tipos_erro.items()):
            w.writerow([tipo, n])
        w.writerow([])

        w.writerow(["=== CLASSIFICAÇÃO POR EXAME (Bloco 1) ==="])
        w.writerow(["Paciente", "Data", "Nome do Exame", "Decisão RPA",
                    "Autorizado", "Classificação"])
        for d in detalhe:
            w.writerow([d.get("paciente", ""), d.get("data_exame", ""),
                        d.get("nome_exame", ""), d.get("decisao", ""),
                        _sim_nao(d.get("autorizado")), d.get("classificacao", "")])
    return path


def _sim_nao(v):
    if v is None:
        return ""
    return "sim" if v else "não"


# ---------------------------------------------------- modo --gerar-gabarito
def gerar_gabarito(runs, gabarito_path):
    # Coleta um exame único por chave, com a melhor decisão observada.
    # Regenera o gabarito do zero já filtrado pela janela 2024-2025: exames fora
    # da janela (antigos, 2026+, "Desconhecido") não entram no arquivo.
    unicos = {}
    fora = 0
    for run in runs:
        for ev in dedup_exames(run.get("exames", [])):
            if not _dentro_janela(ev.get("data_exame")):
                fora += 1
                continue
            chave = _chave(ev.get("paciente"), ev.get("data_exame"), ev.get("nome_exame"))
            atual = unicos.get(chave)
            if atual is None or (PRIORIDADE_DECISAO.get(ev.get("decisao"), 0)
                                 > PRIORIDADE_DECISAO.get(atual.get("decisao"), 0)):
                unicos[chave] = ev

    # Preserva o autorizado que o usuário já revisou.
    existente = carregar_gabarito(gabarito_path)

    novos = preservados = 0
    linhas = []
    for chave, ev in sorted(unicos.items()):
        if chave in existente and existente[chave] is not None:
            autorizado = 1 if existente[chave] else 0
            preservados += 1
        else:
            autorizado = 1 if ev.get("decisao") in DECISOES_PALPITE_SIM else 0
            novos += 1
        linhas.append({
            "paciente": ev.get("paciente", ""), "cpf": ev.get("cpf", ""),
            "data_exame": ev.get("data_exame", ""), "nome_exame": ev.get("nome_exame", ""),
            "decisao_rpa": ev.get("decisao", ""), "autorizado": autorizado,
        })

    os.makedirs(os.path.dirname(gabarito_path), exist_ok=True)
    # Backup antes de sobrescrever: a regeneração descarta exames fora da janela
    # e pacientes ausentes da telemetria atual, então guarda o estado anterior.
    if os.path.exists(gabarito_path):
        import shutil
        shutil.copy(gabarito_path, gabarito_path + ".bak")
    cols = ["paciente", "cpf", "data_exame", "nome_exame", "decisao_rpa", "autorizado"]
    with open(gabarito_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for l in linhas:
            w.writerow(l)
    print(f"Gabarito atualizado: {gabarito_path}")
    print(f"  {len(linhas)} exames unicos (janela {EXAM_YEAR_CUTOFF}-{EXAM_YEAR_MAX}) | "
          f"{novos} novos (palpite do RPA) | {preservados} preservados (ja revisados) | "
          f"{fora} fora da janela (descartados)")
    print("  Revise a coluna 'autorizado' (1/0) - foque nos 'ignorado_irrelevante'.")


# ------------------------------------------------- modo --validar-gabarito
def _celulas_por_coluna(xlsx_path):
    """Lê o .xlsx (stdlib, sem openpyxl) e devolve {coluna: {linha: texto}}.
    O termo é uma matriz: linha 1 = paciente por coluna; linhas abaixo = laudos."""
    z = zipfile.ZipFile(xlsx_path)
    ss_xml = z.read("xl/sharedStrings.xml").decode("utf-8")
    strings = [re.sub(r"<[^>]+>", "", s) for s in
               re.findall(r"<si>(.*?)</si>", ss_xml, re.S)]

    def unesc(s):
        return (s.replace("&amp;", "&").replace("&lt;", "<")
                 .replace("&gt;", ">").replace("&#10;", "\n"))
    strings = [unesc(s) for s in strings]

    sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
    cols = {}
    cell_re = re.compile(
        r'<c r="([A-Z]+)(\d+)"(?:[^>]*t="([^"]*)")?[^>]*>'
        r'(?:<v>(.*?)</v>|<is><t[^>]*>(.*?)</t></is>)?</c>', re.S)
    for col, row, t, v, inl in cell_re.findall(sheet):
        if t == "s" and v != "":
            val = strings[int(v)]
        elif inl:
            val = unesc(inl)
        elif v != "":
            val = v
        else:
            continue
        cols.setdefault(col, {})[int(row)] = val
    return cols


def carregar_termo(xlsx_path):
    """Termo de consentimento -> {paciente_norm: Counter(categoria -> qtd)}.
    Cada coluna é um paciente (linha 1); cada célula abaixo é um laudo cujo texto
    lista modalidades (uma por linha). Soma as ocorrências por categoria."""
    if not os.path.exists(xlsx_path):
        return {}
    cols = _celulas_por_coluna(xlsx_path)
    termo = {}
    for col, celulas in cols.items():
        nome = celulas.get(1)
        if not nome:
            continue
        cont = termo.setdefault(_norm(nome), Counter())
        for row, texto in celulas.items():
            if row == 1:
                continue
            for linha in re.split(r"[\r\n]+", texto):
                if not linha.strip():
                    continue
                for cat in _categorias(linha):
                    cont[cat] += 1
    return termo


def validar_gabarito(gabarito, termo):
    """Confere a CONFORMIDADE do gabarito.csv contra o termo (.xlsx), por
    paciente e por modalidade, considerando só a janela. Não altera arquivos."""
    if not termo:
        print(f"AVISO: termo vazio/inexistente. Nada a validar.")
        return

    # Conta autorizado=1 por paciente/categoria dentro da janela.
    autorizados = {}
    for (pac, data, nome), aut in gabarito.items():
        if not aut or not _dentro_janela(data):
            continue
        cont = autorizados.setdefault(pac, Counter())
        for cat in _categorias(nome):
            cont[cat] += 1

    print("\n" + "=" * 78)
    print(f"VALIDACAO DO GABARITO x TERMO (janela {EXAM_YEAR_CUTOFF}-{EXAM_YEAR_MAX})")
    print("=" * 78)

    avisos = Counter()
    pacientes = sorted(set(termo) | set(autorizados))
    tot_esperado, tot_autorizado = Counter(), Counter()
    for pac in pacientes:
        esp = termo.get(pac, Counter())
        aut = autorizados.get(pac, Counter())
        for c in CATEGORIAS:
            tot_esperado[c] += esp.get(c, 0)
            tot_autorizado[c] += aut.get(c, 0)

        if esp and not aut:
            print(f"\n[COBERTURA] {pac}: esperado no termo {dict(esp)}, "
                  f"mas 0 autorizado no gabarito (janela).")
            avisos["sem_cobertura"] += 1
            continue
        if aut and not esp:
            print(f"\n[FORA DO TERMO] {pac}: {dict(aut)} autorizado(s) no gabarito, "
                  f"mas paciente ausente/vazio no termo.")
            avisos["fora_do_termo"] += 1
            continue

        difs = []
        for c in CATEGORIAS:
            e, a = esp.get(c, 0), aut.get(c, 0)
            if e > a:
                difs.append(f"{c}: faltam {e - a} (esperado {e}, autorizado {a}) [possivel FN]")
                avisos["deficit"] += 1
            elif a > e:
                difs.append(f"{c}: {a - e} a mais (esperado {e}, autorizado {a}) [possivel FP]")
                avisos["excesso"] += 1
        if difs:
            print(f"\n[DIVERGENCIA] {pac}:")
            for d in difs:
                print(f"    - {d}")
        else:
            print(f"\n[OK] {pac}: bate ({dict(aut)}).")

    print("\n" + "-" * 78)
    print(f"Pacientes conferidos: {len(pacientes)}")
    for c in CATEGORIAS:
        print(f"  {c:<11} esperado(termo)={tot_esperado[c]:>3}  "
              f"autorizado(gabarito)={tot_autorizado[c]:>3}")
    print(f"Avisos: {dict(avisos) or 'nenhum'}")
    print("=" * 78)


# ---------------------------------------------------------------- console
def imprimir_tabela(linhas):
    cols = ["run_id", "VP", "FP", "FN", "VN", "Acuracia", "Precisao", "Revocacao",
            "F1", "Taxa_Login", "Taxa_Busca", "Taxa_Download_completo"]
    print("\n" + "=" * 100)
    print("METRICAS REFINADAS - Bloco 1 (Extracao) + Bloco 3 (Etapas)")
    print("=" * 100)
    print(" | ".join(f"{c:>10}" for c in cols))
    print("-" * 100)
    media = linha_media(linhas); media["run_id"] = "Média"
    for l in sorted(linhas, key=lambda x: x["run_id"]) + [media]:
        print(" | ".join(f"{str(_fmt(l.get(c))):>10}" for c in cols))
    print("=" * 100)


def main():
    ap = argparse.ArgumentParser(description="Refino das métricas do RPA (Blocos 1 e 3).")
    ap.add_argument("--telemetria", default=TELEMETRIA_DIR)
    ap.add_argument("--gabarito", default=GABARITO_CSV)
    ap.add_argument("--saida", default=SAIDA_DIR)
    ap.add_argument("--termo", default=TERMO_XLSX)
    ap.add_argument("--gerar-gabarito", action="store_true",
                    help="Cria/atualiza o gabarito por exame (só janela) e sai.")
    ap.add_argument("--validar-gabarito", action="store_true",
                    help="Confere a conformidade do gabarito contra o termo (.xlsx) e sai.")
    args = ap.parse_args()

    if args.validar_gabarito:
        gabarito = carregar_gabarito(args.gabarito)
        termo = carregar_termo(args.termo)
        validar_gabarito(gabarito, termo)
        return 0

    runs = carregar_runs(args.telemetria)
    if not runs:
        print(f"Nenhuma telemetria encontrada em {args.telemetria}")
        return 1

    if args.gerar_gabarito:
        gerar_gabarito(runs, args.gabarito)
        return 0

    gabarito = carregar_gabarito(args.gabarito)
    if not gabarito:
        print(f"AVISO: gabarito vazio/inexistente ({args.gabarito}). Bloco 1 saira "
              f"como SEM_GABARITO. Rode antes: python calcular_metricas.py --gerar-gabarito")

    linhas = []
    for run in runs:
        linha, detalhe, tipos_erro = processar_run(run, gabarito)
        linhas.append(linha)
        p_det = escrever_detalhado(run, linha, detalhe, tipos_erro, args.saida)
        print(f"  detalhado -> {p_det}")

    p_geral = escrever_geral(linhas, args.saida)
    print(f"  geral     -> {p_geral}")
    imprimir_tabela(linhas)
    return 0


if __name__ == "__main__":
    sys.exit(main())
