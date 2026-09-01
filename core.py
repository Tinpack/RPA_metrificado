import os
import time
import re
import shutil
import difflib
import requests
import pandas as pd
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import gspread

from data_manager import (
    remove_accents, normalize_name, read_download_history,
    write_download_history,
)
from config import (
    USER_FIELD_SELECTOR, PASS_FIELD_SELECTOR, LOGIN_BUTTON_SELECTOR,
    SITE_URL, USER, PASS, DOWNLOAD_DIR, SEARCH_BAR_SELECTOR, GOOGLE_CREDS_FILE,
    EXAM_YEAR_CUTOFF, EXAM_YEAR_MAX, EXAM_EXCLUDE_KEYWORDS, EXAM_REPORT_EXCLUDE_MARKERS,
    SEARCH_TIMEOUT, SEARCH_NOT_FOUND_GRACE, SEARCH_SCROLL_CICLOS, STUDY_WAIT_TIMEOUT,
    LAUDO_WAIT_TIMEOUT, REPORT_POPUP_TIMEOUT, REPORT_POPUP_RETRIES,
    DEDUP_SIMILARIDADE, DEDUP_JANELA_DIAS, DEDUP_LOG_RATIO_MIN, DEBUG_DEDUP,
    EXAM_MODALIDADES_PROIBIDAS, EXAM_PROCEDIMENTOS_PROIBIDOS,
    EXAM_REPORT_RM_MARKERS, EXAM_REPORT_HEADER_DELIM, EXAM_REPORT_HEADER_MAX,
)


def _hash8(texto: str) -> str:
    # [DIAGNÓSTICO] Identidade curta do laudo, para comparar capturas entre si sem
    # jogar conteúdo clínico no CloudWatch.
    import hashlib
    return hashlib.sha1((texto or "").encode("utf-8")).hexdigest()[:8]


def check_exam_date(date_str):
    try:
        ano = int(date_str.split(" ")[0].split("/")[2])
        return EXAM_YEAR_CUTOFF <= ano <= EXAM_YEAR_MAX
    except (ValueError, IndexError, AttributeError):
        return False


def is_relevant_exam(exam_text: str) -> bool:
    normalized = remove_accents(exam_text).upper()
    if any(excl in normalized for excl in EXAM_EXCLUDE_KEYWORDS):
        return False
    # Denylist ANTES das palavras-chave: modalidade não autorizada (ressonância,
    # tomografia, cintilografia...) e procedimentos (biópsia, punção) casam
    # "MAMA"/"BREAST" e passariam pelo filtro de palavra-chave abaixo.
    if re.search(EXAM_MODALIDADES_PROIBIDAS, normalized):
        return False
    if re.search(EXAM_PROCEDIMENTOS_PROIBIDOS, normalized):
        return False
    target_keywords = ["MAMA", "MAMO", "MAMMO", "MMG", "BREAST", "AXILA", "IMPLANT", "NODULO", "ECOGRAFIA", "ULTRASSONOGRAFIA"]
    return any(keyword in normalized for keyword in target_keywords)


def _texto_laudo(page) -> str:
    # O laudo é renderizado num frame; junta o texto da página + todos os frames.
    textos = []
    try:
        textos.append(page.inner_text("body"))
    except Exception:
        pass
    for frame in page.frames:
        try:
            textos.append(frame.inner_text("body"))
        except Exception:
            pass
    return " ".join(textos)


def _frame_do_laudo(page):
    # [DIAGNÓSTICO] Aponta qual frame contém o laudo (maior frame, excluindo o
    # frame principal da UI #0). Retorna (indice, texto) ou (None, "").
    melhor_i, melhor_t = None, ""
    for i, fr in enumerate(page.frames):
        if i == 0:
            continue  # frame principal = UI do portal (lista de cards, abas)
        try:
            t = fr.inner_text("body")
        except Exception:
            continue
        if len(t) > len(melhor_t):
            melhor_i, melhor_t = i, t
    return melhor_i, melhor_t


def _dump_frames(page, motivo):
    """[DIAGNÓSTICO] Inventário de TODOS os frames no instante da captura.

    Existe para separar duas causas que hoje ficam idênticas no log: o frame do laudo
    estava VAZIO, ou `_frame_do_laudo` escolheu o frame ERRADO? Ele devolve o maior
    texto entre os frames (pulando o #0, que é a UI): se o relatório ainda não anexou,
    sobra uma casca pequena — e é assim que nasce a captura de 31 chars em branco.

    Imprime índice, tamanho do texto (cru e normalizado) e URL de cada frame; para os
    frames pequenos, despeja `repr()` do texto e um trecho do HTML, que é onde aparece
    spinner/placeholder. Só roda com DEBUG_DEDUP e não influencia decisão nenhuma."""
    try:
        frames = list(page.frames)
    except Exception as e:
        print(f"    [RPA][frames] nao consegui listar frames: {type(e).__name__}")
        return
    print(f"    [RPA][frames] === {motivo}: {len(frames)} frame(s) ===")
    for i, fr in enumerate(frames):
        try:
            texto = fr.inner_text("body")
        except Exception as e:
            print(f"    [RPA][frames]   #{i} <falha ao ler body: {type(e).__name__}> "
                  f"url={(getattr(fr, 'url', '') or '')[:100]}")
            continue
        norm = _normalizar_laudo(texto)
        print(f"    [RPA][frames]   #{i} chars={len(texto)} norm={len(norm)} "
              f"hash={_hash8(texto)} url={(fr.url or '')[:100]}")
        if len(texto) < 200:  # candidato a casca: mostra o conteúdo exato
            print(f"    [RPA][frames]      texto={texto[:200]!r}")
            try:
                html = fr.content()
                print(f"    [RPA][frames]      html_len={len(html)} inicio={html[:300]!r}")
            except Exception as e:
                print(f"    [RPA][frames]      <falha ao ler html: {type(e).__name__}>")
    print(f"    [RPA][frames] === fim do dump ===")


def _log_laudo(page):
    # Loga a decisão do marcador sobre o laudo (título extraído + veredito). Útil
    # para auditar por que um laudo foi mantido/pulado. Nenhuma decisão depende disto.
    idx, txt = _frame_do_laudo(page)
    if idx is None:
        print("    [RPA][laudo] frame do laudo nao encontrado")
        return
    print(f"    [RPA][laudo] valido={_laudo_tem_titulo_valido(txt)} "
          f"titulo='{_titulo_laudo_principal(txt)[:70]}'")


def _texto_laudo_frame(page) -> str:
    # Só o conteúdo do laudo (frames), SEM o body da página: o body traz toda a
    # UI do portal (lista de cards, abas) e dois exames diferentes ficariam
    # ~95% parecidos só por isso, inviabilizando a comparação por similaridade.
    # Heurística: o maior texto entre os frames é o relatório.
    melhor = ""
    for frame in page.frames:
        try:
            t = frame.inner_text("body")
        except Exception:
            continue
        if len(t) > len(melhor):
            melhor = t
    return melhor


def _laudo_frame_estavel(page, timeout: float = LAUDO_WAIT_TIMEOUT) -> str:
    # Texto do FRAME do laudo (via _frame_do_laudo, que exclui a UI do portal),
    # capturado só depois de ESTABILIZAR (~0,5s sem mudar). É a chave da dedup:
    # ler solto/parcial fazia o ratio da MESMA cópia oscilar (0,98 numa run, <0,90
    # noutra) e cópia real vazava (caso Consuelo). Estabilizar deixa a captura
    # determinística -> cópia real ~0,98 estável, stub ~0,89 -> 0,90 separa os dois.
    t0 = time.time()
    ultimo = None
    estavel_desde = None
    amostras = 0

    def _finalizar(texto, estabilizou, idx=None):
        # [DIAGNÓSTICO] Ponto único de saída, para o log valer nos dois caminhos.
        if DEBUG_DEDUP:
            print(f"    [RPA][estavel] estabilizou={estabilizou} "
                  f"elapsed={time.time() - t0:.2f}s amostras={amostras} "
                  f"frame={idx} chars={len(texto or '')} hash={_hash8(texto)}")
            # Sem conteúdo útil = o caso que faz a dedup falhar (ratio 0.0 -> cópia
            # baixada -> FP). Só aqui vale o custo do dump completo dos frames.
            if len(_normalizar_laudo(texto)) < 20:
                print(f"    [RPA][estavel] CAPTURA SEM CONTEUDO texto={(texto or '')[:200]!r}")
                _dump_frames(page, "captura sem conteudo util")
        return texto

    while time.time() - t0 < timeout:
        idx, t = _frame_do_laudo(page)
        amostras += 1
        if t and t == ultimo:
            if estavel_desde is not None and (time.time() - estavel_desde) >= 0.5:
                return _finalizar(t, True, idx)
            if estavel_desde is None:
                estavel_desde = time.time()
        else:
            estavel_desde = None
        ultimo = t
        time.sleep(0.1)
    # Estourou o teto sem estabilizar: devolve o que houver (possivelmente parcial).
    # [DIAGNÓSTICO] Este caminho era MUDO — e continua sendo o canário: hoje ele
    # nunca dispara (0 em 191 capturas medidas).
    idx_final, final = _frame_do_laudo(page)
    return _finalizar(final, False, idx_final)


def _normalizar_laudo(texto: str) -> str:
    return " ".join(remove_accents(texto or "").upper().split())


def _ratio_laudo(a: str, b: str, limiar: float) -> float:
    # Similaridade entre dois laudos normalizados. quick_ratio() é um limite
    # SUPERIOR barato: se já fica abaixo do limiar, nem calcula o ratio real.
    # [DIAGNÓSTICO] Os dois 0.0 abaixo têm causas MUITO diferentes (captura vazia vs.
    # laudos realmente distintos) e eram indistinguíveis no log.
    if not a or not b:
        if DEBUG_DEDUP:
            print(f"    [RPA][dedup]   ratio=0.000 motivo=vazio "
                  f"(anterior={len(a or '')} chars, atual={len(b or '')} chars)")
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b)
    qr = sm.quick_ratio()
    if qr < limiar:
        if DEBUG_DEDUP:
            print(f"    [RPA][dedup]   ratio=0.000 motivo=quick_ratio<{limiar} (qr={qr:.3f})")
        return 0.0
    return sm.ratio()


def _data_card(date_str):
    # "DD/MM/AAAA HH:MM ..." -> datetime (só a data). None se não parsear.
    try:
        return datetime.strptime(date_str.split(" ")[0], "%d/%m/%Y")
    except (ValueError, IndexError, AttributeError):
        return None


def _dentro_janela_dias(d1, d2, dias: int) -> bool:
    # Sem data confiável em algum dos lados -> não deduplica (conservador).
    if d1 is None or d2 is None:
        return False
    return abs((d1 - d2).days) <= dias


def _texto_indica_skip(texto: str) -> bool:
    markers = [remove_accents(m).upper() for m in EXAM_REPORT_EXCLUDE_MARKERS]
    blob = remove_accents(texto).upper()
    return any(marker in blob for marker in markers)


def _cabecalho_laudo(texto: str) -> str:
    # Só o topo do laudo (cidade/data, paciente, médico e o TÍTULO do exame).
    # "Informação Clínica" marca o início do corpo — e o corpo cita outras
    # modalidades (um laudo de RM menciona "Ecografia mamária" ali), então
    # procurar modalidade abaixo desse ponto daria falso positivo/negativo.
    t = " ".join(remove_accents(texto or "").upper().split())
    corte = t.find(EXAM_REPORT_HEADER_DELIM)
    return t[:corte] if corte != -1 else t[:EXAM_REPORT_HEADER_MAX]


def _titulo_laudo(texto: str) -> str:
    # Título/tipo do exame. Âncora: a linha logo após "Dr. (a): ...", posição
    # estável nos laudos do portal. Usado só para DIAGNÓSTICO (log) por enquanto:
    # o corpo do laudo não é confiável para classificar (um laudo diagnóstico
    # válido pode trazer "Prezado(a) colega", e um de RM cita "Ecografia").
    linhas = [l.strip() for l in (texto or "").splitlines() if l.strip()]
    for i, l in enumerate(linhas):
        if remove_accents(l).upper().startswith("DR"):
            return remove_accents(linhas[i + 1]).upper() if i + 1 < len(linhas) else ""
    return remove_accents(" ".join(linhas[:4])).upper()


def _titulo_laudo_principal(texto: str) -> str:
    # O TÍTULO do exame fica no topo do laudo, ANTES do início do corpo — que
    # começa por um destes cabeçalhos: "Prezado(a) colega" (cartas), "Informação
    # Clínica" ou "Resumo Clínico" (laudos diagnósticos). Delimitador que existe
    # com certeza. Pega SÓ esse primeiro trecho (o título de verdade) — não varre
    # o corpo: a carta de biópsia cita "US axilar prévia..." no corpo, e olhar
    # janelas do corpo resgatava a biópsia por engano (caso Rosangela).
    t = " ".join(remove_accents(texto or "").upper().split())
    cortes = [p for d in ("PREZADO", "INFORMACAO CLINICA", "RESUMO CLINICO")
              for p in [m.start() for m in re.finditer(d, t)]]
    return t[:min(cortes)] if cortes else t[:200]


def _laudo_tem_titulo_valido(texto: str) -> bool:
    # True se o TÍTULO principal for um exame diagnóstico válido. Resgata o laudo
    # que traz "Prezado(a) colega" no corpo mas cujo título é exame legítimo
    # (Janete, Virginia pág.1); barra biópsia/pré-op puros (título é o procedimento).
    return is_relevant_exam(_titulo_laudo_principal(texto))


def _laudo_eh_ressonancia(texto: str) -> bool:
    cabecalho = _cabecalho_laudo(texto)
    return any(m in cabecalho for m in EXAM_REPORT_RM_MARKERS)


def report_should_skip(page) -> bool:
    return _texto_indica_skip(_texto_laudo(page))


def esperar_laudo_carregar(page, texto_anterior: str, timeout: float = LAUDO_WAIT_TIMEOUT) -> str:
    # Espera o laudo MUDAR em relação ao anterior e ESTABILIZAR (~0,5s sem
    # mudança). Mede ~1,5s na prática; substitui o time.sleep(3) fixo.
    t0 = time.time()
    mudou = False
    ultimo = None
    estavel_desde = None
    while time.time() - t0 < timeout:
        txt = _texto_laudo(page)
        if not mudou and txt and txt != texto_anterior and len(txt) > 30:
            mudou = True
        if mudou:
            if txt == ultimo:
                if estavel_desde is not None and (time.time() - estavel_desde) >= 0.5:
                    return txt
                if estavel_desde is None:
                    estavel_desde = time.time()
            else:
                estavel_desde = None
        ultimo = txt
        time.sleep(0.1)
    return _texto_laudo(page)


def download_pdf_from_url(url: str, cookies: list, save_path: str) -> bool:
    try:
        session = requests.Session()
        for cookie in cookies:
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie.get('domain', ''))
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/pdf,*/*'}
        response = session.get(url, headers=headers, timeout=60, stream=True)
        if response.status_code == 200:
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        print(f"ERRO: Status {response.status_code} ao baixar PDF de {url}")
        return False
    except Exception as e:
        print(f"ERRO: Falha no download direto do PDF: {e}")
        return False


def extract_pdf_from_blob_url(page, save_path: str) -> bool:
    try:
        pdf_base64 = page.evaluate("""
            async () => {
                const iframe = document.querySelector('iframe');
                const target = (window.location.href.startsWith('blob:')) ? window.location.href : (iframe && iframe.src.includes('blob:') ? iframe.src : null);
                if (target) {
                    const response = await fetch(target);
                    const blob = await response.blob();
                    return new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result.split(',')[1]);
                        reader.readAsDataURL(blob);
                    });
                }
                return null;
            }
        """)
        if pdf_base64:
            import base64
            pdf_bytes = base64.b64decode(pdf_base64)
            if pdf_bytes[:4] == b'%PDF':
                with open(save_path, 'wb') as f:
                    f.write(pdf_bytes)
                return True
        return False
    except Exception as e:
        print(f"ERRO: Falha ao extrair PDF do blob: {e}")
        return False


def convert_html_to_pdf_playwright(context, html_url: str, save_path: str) -> bool:
    pdf_page = None
    try:
        pdf_page = context.new_page()
        pdf_page.add_init_script("window.print = function() { return false; };")
        pdf_page.goto(html_url, timeout=45000)
        pdf_page.wait_for_load_state('networkidle', timeout=20000)
        time.sleep(5)
        pdf_page.pdf(path=save_path, format='A4', print_background=True)
        pdf_page.close()
        return os.path.exists(save_path)
    except Exception as e:
        print(f"ERRO: Falha ao converter HTML para PDF: {e}")
        if pdf_page:
            try:
                pdf_page.close()
            except Exception:
                pass
        return False


def is_html_report(url: str) -> bool:
    try:
        return parse_qs(urlparse(url).query).get('pdf', ['true'])[0].lower() == 'false'
    except Exception:
        return False


def read_patients_from_gsheets(sheet_url: str) -> dict:
    try:
        gc = gspread.service_account(filename=GOOGLE_CREDS_FILE)
        planilha = gc.open_by_url(sheet_url).sheet1
        df = pd.DataFrame(planilha.get_all_records())
        pacientes_filtrados = [{"sheet_row": index + 2, "nome": str(row.get("Por gentileza, informe o seu nome completo:", "")).strip(), "cpf": str(row.get("CPF", "")).strip()}
                               for index, row in df.iterrows() if str(row.get("STATUS", "0")) == "1"]
        return {"pacientes": pacientes_filtrados}
    except Exception as e:
        return {"error": str(e)}


def update_sheet_status(sheet_url, row_index, status_value):
    try:
        gc = gspread.service_account(filename=GOOGLE_CREDS_FILE)
        sh = gc.open_by_url(sheet_url).sheet1
        headers = sh.row_values(1)
        if "STATUS" in headers:
            col_idx = headers.index("STATUS") + 1
            sh.update_cell(row_index, col_idx, status_value)
    except Exception:
        pass


def do_login(page):
    page.goto(SITE_URL, timeout=60000)
    try:
        page.locator(f"#{USER_FIELD_SELECTOR}").wait_for(state="visible", timeout=5000)
    except Exception:
        return
    page.fill(f"#{USER_FIELD_SELECTOR}", USER)
    page.fill(f"#{PASS_FIELD_SELECTOR}", PASS)
    with page.expect_navigation():
        page.click(f"#{LOGIN_BUTTON_SELECTOR}")


def _search_bar_visible(page) -> bool:
    try:
        search = page.locator(f"#{SEARCH_BAR_SELECTOR}")
        return search.count() > 0 and search.first.is_visible()
    except Exception:
        return False


def reset_para_busca(page):
    # Reset LEVE (ação interna do app, ~1s): volta para a aba "Localizar
    # paciente" (#mainTab-0) e fecha as guias de paciente abertas, evitando
    # acúmulo. NÃO recarrega nem re-loga — recarregar a home não funciona:
    # o portal redireciona default.aspx para o WebLogin a cada navegação.
    try:
        page.locator("#mainTab-0").click(timeout=5000)
        for _ in range(15):
            fechar = page.locator("#mainTabset li:not(#mainTab-0) .mtbCloseBtn_normal")
            if fechar.count() == 0:
                break
            try:
                fechar.first.click(timeout=2000)
                time.sleep(0.15)
            except Exception:
                break
        search = page.locator(f"#{SEARCH_BAR_SELECTOR}")
        search.wait_for(state="visible", timeout=8000)
        search.first.fill("")
        return
    except Exception:
        pass

    # Fallback: sessão caiu -> re-login completo.
    try:
        do_login(page)
        page.locator(f"#{SEARCH_BAR_SELECTOR}").wait_for(state="visible", timeout=15000)
        page.locator(f"#{SEARCH_BAR_SELECTOR}").first.fill("")
    except Exception:
        pass


def _ler_linhas_resultado(page):
    try:
        nomes = page.locator("#patientsTableBody tr td:nth-child(1)").all_inner_texts()
    except Exception:
        return None
    try:
        ids = page.locator("#patientsTableBody tr td:nth-child(2)").all_inner_texts()
    except Exception:
        ids = []
    linhas = []
    for i, nome in enumerate(nomes):
        linhas.append((nome, ids[i] if i < len(ids) else ""))
    return linhas


def _rolar_resultados(page):
    # Backstop: rola até a última linha renderizada. A RAIZ do "só 15 linhas" era o
    # viewport baixo (corrigido no new_context em main.py); isto fica só como
    # empurrão extra caso ainda falte renderizar alguma linha.
    try:
        page.locator("#patientsTableBody tr").last.scroll_into_view_if_needed(timeout=1000)
    except Exception:
        pass


def _badge_resultado(page):
    # Conta resultados pelo badge da aba: "Localizar paciente (N)".
    try:
        txt = page.locator("#mainTab-0-tabCaptionPlace").inner_text(timeout=1000)
    except Exception:
        return None
    m = re.search(r"\((\d+)\)", txt)
    return int(m.group(1)) if m else None


def buscar_paciente(page, nome_paciente: str, timeout: float = SEARCH_TIMEOUT,
                    grace: float = SEARCH_NOT_FOUND_GRACE) -> dict:
    alvo = normalize_name(nome_paciente)
    search = page.locator(f"#{SEARCH_BAR_SELECTOR}")
    search.wait_for(state="visible", timeout=15000)
    search.click()
    search.fill("")
    search.fill(remove_accents(nome_paciente))
    search.press("Enter")

    inicio = time.time()
    deadline = inicio + timeout
    viu_linhas = False
    linhas_vistas = []       # maior conjunto de linhas já lido (p/ diagnóstico)
    ultimo_total = 0         # p/ detectar quando a lista para de crescer
    ciclos_sem_crescer = 0
    while time.time() < deadline:
        linhas = _ler_linhas_resultado(page)
        if linhas:
            viu_linhas = True
            if len(linhas) > len(linhas_vistas):
                linhas_vistas = linhas
            for idx, (nome_txt, id_txt) in enumerate(linhas):
                if normalize_name(nome_txt) == alvo:
                    return {"index": idx, "id_cell": id_txt.strip(), "viu_linhas": True}
            # Nenhum match: a lista costuma estar INCOMPLETA no DOM (só as linhas
            # renderizadas vêm no seletor — o usuário vê 17, o seletor devolve 15).
            # Rola para trazer as próximas e só desiste quando a contagem PARA de
            # crescer. NÃO usar o badge como guarda: ele devolve a contagem de
            # abas (leu "1" com 15 linhas), o que desativava o scroll por completo.
            if len(linhas) > ultimo_total:
                ultimo_total = len(linhas)
                ciclos_sem_crescer = 0
            else:
                ciclos_sem_crescer += 1
            if ciclos_sem_crescer < SEARCH_SCROLL_CICLOS:
                _rolar_resultados(page)
        # Saída rápida: já deu tempo da busca rodar e o badge mostra 0 com
        # a tabela vazia -> paciente não existe (evita esperar o timeout todo).
        elif (time.time() - inicio) > grace and _badge_resultado(page) == 0:
            return {"index": None, "id_cell": None, "viu_linhas": viu_linhas}
        time.sleep(0.25)

    # [DIAGNÓSTICO] Não achou: mostra o que foi lido para distinguir "lista
    # incompleta" (lidas < badge) de "grafia diferente" (lidas == badge).
    if viu_linhas:
        print(f"    [RPA][busca] alvo='{alvo}' | {len(linhas_vistas)} linha(s) lidas "
              f"(apos scroll; badge={_badge_resultado(page)} — nao confiavel):")
        for i, (nome_txt, _) in enumerate(linhas_vistas, 1):
            print(f"        {i:02d} '{nome_txt.strip()[:52]}' -> '{normalize_name(nome_txt)[:52]}'")

    return {"index": None, "id_cell": None, "viu_linhas": viu_linhas}


def process_patient_exams(page, context, nome_paciente: str, cpf_paciente: str, report) -> dict:
    print(f"Iniciando processamento de {nome_paciente}")
    # Diretório de staging do PDF antes do move para DOWNLOAD_DIR. Precisa
    # existir e ser gravável em qualquer ambiente — usar ~/Downloads quebrava
    # no Docker (home = /root, sem pasta Downloads -> open() falhava silencioso
    # e todo exame virava extract_pdf_failed). Subpasta de DOWNLOAD_DIR funciona
    # tanto no Windows quanto no container (volume /data/downloads montado).
    TEMP_DOWNLOADS = os.path.join(DOWNLOAD_DIR, ".tmp")
    os.makedirs(TEMP_DOWNLOADS, exist_ok=True)

    stats = {
        "status": "success",
        "alvos_encontrados": 0,
        "sucesso_rpa": 0,
        "falhas": 0,
    }

    try:
        resultado_busca = buscar_paciente(page, nome_paciente)
        report.registrar_busca(nome_paciente, resultado_busca["index"] is not None)  # [MÉTRICAS]

        if resultado_busca["index"] is None:
            stats["status"] = "not_found"
            report.registrar_paciente_nao_encontrado(nome_paciente)
            motivo = (
                "Nome do paciente não bateu com nenhuma linha da tabela de busca."
                if resultado_busca["viu_linhas"]
                else "Paciente não retornado pela busca no portal."
            )
            report.registrar_erro(
                paciente=nome_paciente, cpf=cpf_paciente,
                data_exame="", nome_exame="",
                motivo=motivo,
                etapa="patient_not_found",
            )
            return stats

        id_cell = resultado_busca["id_cell"]
        page.locator("#patientsTableBody tr").nth(resultado_busca["index"]).locator("td:nth-child(1)").click(timeout=10000)

        try:
            page.locator(".studyControlPlace").first.wait_for(state='visible', timeout=STUDY_WAIT_TIMEOUT)
        except Exception:
            pass  # paciente pode não ter exames visíveis -> num_exams = 0 abaixo

        num_exams = page.locator(".studyControlPlace").count()
        history = read_download_history()
        ID_PACIENTE = id_cell.zfill(16)

        exames_alvo = []
        exames_vistos_nesta_sessao = set()

        for i in range(num_exams):
            cont = page.locator(".studyControlPlace").nth(i)
            texto_completo_card = cont.inner_text().strip()
            html_content = cont.inner_html().lower()

            try:
                date_text = cont.locator(".sccDate").inner_text().strip()
            except Exception:
                date_text = "Data Oculta"

            name_str_limpo = texto_completo_card.replace('\n', ' ').strip()

            if "apenas imagens" in html_content:
                report.registrar_exame_visto(  # [MÉTRICAS]
                    paciente=nome_paciente, cpf=cpf_paciente, data_exame=date_text,
                    nome_exame=name_str_limpo, decisao="ignorado_apenas_imagens")
                continue

            if check_exam_date(date_text) and is_relevant_exam(texto_completo_card):
                exam_history_id = f"{nome_paciente}-{ID_PACIENTE}-{date_text}-{name_str_limpo}"

                if exam_history_id not in history and exam_history_id not in exames_vistos_nesta_sessao:
                    exames_vistos_nesta_sessao.add(exam_history_id)
                    report.registrar_exame_visto(  # [MÉTRICAS] decisão provisória, atualizada no download
                        paciente=nome_paciente, cpf=cpf_paciente, data_exame=date_text,
                        nome_exame=name_str_limpo, decisao="alvo")
                    exames_alvo.append({
                        "index": i,
                        "date": date_text,
                        "name": name_str_limpo,
                        "history_id": exam_history_id,
                    })
                elif exam_history_id in history:  # [MÉTRICAS] já coletado em execução anterior
                    report.registrar_exame_visto(
                        paciente=nome_paciente, cpf=cpf_paciente, data_exame=date_text,
                        nome_exame=name_str_limpo, decisao="ja_no_historico")
            else:  # [MÉTRICAS] exame fora do escopo: distingue o motivo p/ o universo VP/FP/FN/VN
                decisao = "ignorado_data" if not check_exam_date(date_text) else "ignorado_irrelevante"
                report.registrar_exame_visto(
                    paciente=nome_paciente, cpf=cpf_paciente, data_exame=date_text,
                    nome_exame=name_str_limpo, decisao=decisao)

        stats["alvos_encontrados"] = len(exames_alvo)
        report.registrar_alvos(nome_paciente, len(exames_alvo))
        print(f"    [RPA] Mapeamento interno concluído: {len(exames_alvo)} exames únicos serão baixados.")

        # Vários cards abrem o MESMO laudo e a dedup mantém o primeiro baixado.
        # Tenta primeiro o card com nome descritivo ("MG BREAST MAMOGRAFIA...")
        # em vez do genérico ("** SOMENTE RELATÓRIO **"), para o arquivo salvo
        # ficar com nome útil. sort estável: preserva a ordem dentro do grupo.
        exames_alvo.sort(key=lambda a: 1 if "SOMENTE RELATORIO" in remove_accents(a["name"]).upper() else 0)

        # Dedup por CONTEÚDO do laudo (mesma run/paciente): 1 laudo combinado
        # costuma aparecer em vários cards (MAMO/ECO/AXILA) -> mesmo laudo baixado
        # N vezes. Lista de (texto_normalizado, nome_exame, data) dos laudos
        # efetivamente SALVOS deste paciente.
        laudos_baixados = []

        for alvo in exames_alvo:
            i = alvo["index"]
            date_text = alvo["date"]
            name_str = alvo["name"]
            exam_history_id = alvo["history_id"]

            try:
                data_hoje = datetime.now().strftime("%d-%m-%Y")
                nome_base = f"{remove_accents(nome_paciente)}-{remove_accents(name_str)}-{data_hoje}"
                filename = re.sub(r'[\\/*?:"<>|]', '_', f"{nome_base}.pdf")

                temp_save_path = os.path.join(TEMP_DOWNLOADS, filename)
                final_dest_path = os.path.join(DOWNLOAD_DIR, filename)

                print(f"    [RPA] Baixando: {name_str}")

                laudo_anterior = _texto_laudo(page)
                cont = page.locator(".studyControlPlace").nth(i)
                cont.locator("div[id$='_study']").click(force=True)
                laudo_atual = esperar_laudo_carregar(page, laudo_anterior)

                if _texto_indica_skip(laudo_atual):
                    # "Prezado(a) colega" / pré-op no CORPO não bastam: um exame
                    # diagnóstico válido (ecografia/mamografia) também traz essa
                    # saudação. Decide pelo TÍTULO de cada seção do laudo — se
                    # qualquer uma for exame válido, mantém (ex.: Virginia com
                    # pág.1 ecografia + pág.2 biópsia); só biópsia/pré-op puros
                    # (nenhuma seção válida) são pulados.
                    _, _texto_laudo6 = _frame_do_laudo(page)
                    if _laudo_tem_titulo_valido(_texto_laudo6):
                        print(f"    [RPA] Marcador no corpo, mas título diagnóstico válido — mantido: {name_str}")
                        _log_laudo(page)  # [DIAGNÓSTICO] confirma o título extraído
                        # não pula: segue para RM/dedup/download normalmente
                    else:
                        print(f"    [RPA] Ignorado (carta/procedimento — biópsia/pré-op): {name_str}")
                        _log_laudo(page)  # [DIAGNÓSTICO] título/hash do laudo
                        write_download_history(exam_history_id)
                        report.registrar_ignorado(nome_paciente)
                        report.atualizar_decisao_exame(  # [MÉTRICAS]
                            paciente=nome_paciente, data_exame=date_text,
                            nome_exame=name_str, decisao="ignorado_marcador")
                        try:
                            page.keyboard.press("Escape")
                        except Exception:
                            pass
                        time.sleep(0.3)
                        continue

                # Card sem modalidade no nome pode ser ressonância: decide pelo
                # título no cabeçalho do laudo (o termo não autoriza RM).
                if _laudo_eh_ressonancia(laudo_atual):
                    print(f"    [RPA] Ignorado (laudo de ressonância magnética): {name_str}")
                    write_download_history(exam_history_id)
                    report.registrar_ignorado(nome_paciente)
                    report.atualizar_decisao_exame(  # [MÉTRICAS]
                        paciente=nome_paciente, data_exame=date_text,
                        nome_exame=name_str, decisao="ignorado_ressonancia")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    time.sleep(0.3)
                    continue

                # Cópia do mesmo laudo já baixado nesta run/paciente? -> pula.
                # Compara por similaridade (data e IDs do rodapé mudam entre as
                # cópias, e alguns nem aparecem em todas -> hash exato não serve).
                laudo_norm = _normalizar_laudo(_laudo_frame_estavel(page))
                data_atual = _data_card(date_text)
                dup_nome = None
                dup_ratio = None
                # [DIAGNÓSTICO] Acompanha por que a dedup NÃO casou: hoje um ratio
                # abaixo de DEDUP_LOG_RATIO_MIN some do log e a cópia é baixada.
                _melhor_ratio, _melhor_nome, _fora_janela = 0.0, None, 0
                for texto_ant, nome_ant, data_ant in laudos_baixados:
                    if not _dentro_janela_dias(data_ant, data_atual, DEDUP_JANELA_DIAS):
                        _fora_janela += 1
                        continue
                    ratio = _ratio_laudo(texto_ant, laudo_norm, DEDUP_LOG_RATIO_MIN)
                    if ratio > _melhor_ratio:
                        _melhor_ratio, _melhor_nome = ratio, nome_ant
                    if ratio >= DEDUP_SIMILARIDADE:
                        dup_nome, dup_ratio = nome_ant, ratio
                        break
                    if ratio >= DEDUP_LOG_RATIO_MIN:  # perto do limiar: loga p/ calibrar
                        print(f"    [RPA][dedup] '{name_str}' vs '{nome_ant}': "
                              f"ratio={ratio:.3f} (nao deduplicado)")

                if DEBUG_DEDUP:
                    # Resumo SEMPRE emitido (mesmo com ratio 0.0), que é o caso cego hoje.
                    print(f"    [RPA][dedup] '{name_str}': "
                          f"veredito={'DUP' if dup_nome else 'NAO-DUP'} "
                          f"chars={len(laudo_norm)} hash={_hash8(laudo_norm)} "
                          f"cands={len(laudos_baixados)} fora_janela={_fora_janela} "
                          f"ratio_max={_melhor_ratio:.3f} vs='{(_melhor_nome or '-')[:45]}' "
                          f"limiar={DEDUP_SIMILARIDADE}")

                if dup_nome:
                    print(f"    [RPA] Ignorado (cópia do mesmo laudo já baixado "
                          f"'{dup_nome}' ratio={dup_ratio:.3f}): {name_str}")
                    write_download_history(exam_history_id)
                    report.registrar_ignorado(nome_paciente)
                    report.atualizar_decisao_exame(  # [MÉTRICAS]
                        paciente=nome_paciente, data_exame=date_text,
                        nome_exame=name_str, decisao="ignorado_duplicado")
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    time.sleep(0.3)
                    continue

                file_saved = False
                pdf_url = None
                new_page = None
                relatorio_indisponivel = False
                motivo_falha_extracao = None
                download_method = None

                def handle_new_page(new_pg):
                    try:
                        new_pg.add_init_script("window.print = function() { return false; };")
                    except Exception:
                        pass

                context.on("page", handle_new_page)
                try:
                    # O expect_page às vezes estoura por lentidão do portal
                    # (falha transitória) — tenta de novo antes de desistir.
                    ultimo_erro = None
                    for tentativa in range(REPORT_POPUP_RETRIES + 1):
                        try:
                            with context.expect_page(timeout=15000) as np:
                                page.locator("span[id$='_printBtn'].btnPrint").click(force=True)
                            new_page = np.value
                            break
                        except Exception as e:
                            ultimo_erro = e
                            if tentativa < REPORT_POPUP_RETRIES:
                                print(f"    [RPA] Popup não abriu (tentativa {tentativa + 1}), repetindo: {name_str}")
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass
                                time.sleep(1.0)
                    if new_page is None:
                        raise ultimo_erro
                    new_page.wait_for_load_state('domcontentloaded', timeout=15000)
                    # Espera o iframe do relatório aparecer (em vez de sleep fixo);
                    # sai cedo quando o iframe tem src ou quando o portal sinaliza
                    # indisponibilidade.
                    deadline_popup = time.time() + REPORT_POPUP_TIMEOUT
                    while time.time() < deadline_popup:
                        try:
                            loc = new_page.locator("iframe").first
                            if loc.count() > 0 and (loc.get_attribute("src") or "").strip():
                                break
                        except Exception:
                            pass
                        try:
                            if "não está disponível" in new_page.content().lower():
                                break
                        except Exception:
                            pass
                        time.sleep(0.1)

                    try:
                        conteudo_pagina = new_page.content().lower()
                        if "não está disponível para exibição" in conteudo_pagina or "relatório não está disponível" in conteudo_pagina:
                            relatorio_indisponivel = True
                        else:
                            for frame in new_page.frames:
                                try:
                                    if "não está disponível para exibição" in frame.content().lower():
                                        relatorio_indisponivel = True
                                        break
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    if not relatorio_indisponivel:
                        new_page_url = new_page.url
                        iframe_locator = new_page.locator('iframe#PrintFrame, iframe[src*="ReportService"], iframe').first

                        if iframe_locator.count() > 0:
                            iframe_src = iframe_locator.get_attribute('src')
                            if iframe_src:
                                if iframe_src.startswith('./') or iframe_src.startswith('/'):
                                    pdf_url = f"{urlparse(new_page_url).scheme}://{urlparse(new_page_url).netloc}{iframe_src.replace('./', '/')}"
                                else:
                                    pdf_url = iframe_src

                        is_html = is_html_report(new_page_url)

                        if pdf_url and not is_html:
                            file_saved = download_pdf_from_url(pdf_url, context.cookies(), temp_save_path)
                            if file_saved:
                                with open(temp_save_path, 'rb') as f:
                                    if f.read(4) != b'%PDF':
                                        os.remove(temp_save_path)
                                        file_saved, is_html = False, True
                                if file_saved:
                                    download_method = "IFRAME"

                        if not file_saved and not is_html:
                            try:
                                js_pdf_url = new_page.evaluate("() => { const iframe = document.querySelector('iframe'); return iframe ? iframe.src : null; }")
                                if js_pdf_url:
                                    file_saved = download_pdf_from_url(js_pdf_url, context.cookies(), temp_save_path)
                                    if file_saved:
                                        download_method = "JAVASCRIPT"
                            except Exception:
                                pass

                        if not file_saved:
                            file_saved = extract_pdf_from_blob_url(new_page, temp_save_path)
                            if file_saved:
                                download_method = "BLOB"

                        if not file_saved and (is_html or pdf_url):
                            file_saved = convert_html_to_pdf_playwright(context, pdf_url or new_page_url, temp_save_path)
                            if file_saved:
                                download_method = "HTML->PDF"

                except Exception as e:
                    motivo_falha_extracao = f"Exceção na extração do PDF: {e}"
                    print(f"ERRO: Erro ao extrair PDF: {e}")
                finally:
                    try:
                        context.remove_listener("page", handle_new_page)
                    except Exception:
                        pass
                    if new_page:
                        try:
                            new_page.close()
                        except Exception:
                            pass
                    try:
                        page.keyboard.press("Escape")
                    except Exception:
                        pass
                    time.sleep(0.3)

                if relatorio_indisponivel:
                    stats["falhas"] += 1
                    report.registrar_erro(
                        paciente=nome_paciente, cpf=cpf_paciente,
                        data_exame=date_text, nome_exame=name_str,
                        motivo="Portal informou que o relatório não está disponível para exibição.",
                        etapa="relatorio_indisponivel",
                    )
                    report.atualizar_decisao_exame(  # [MÉTRICAS]
                        paciente=nome_paciente, data_exame=date_text,
                        nome_exame=name_str, decisao="relatorio_indisponivel")
                elif file_saved and os.path.exists(temp_save_path):
                    if os.path.exists(final_dest_path):
                        os.remove(final_dest_path)
                    shutil.move(temp_save_path, final_dest_path)

                    write_download_history(exam_history_id)
                    if laudo_norm:
                        laudos_baixados.append((laudo_norm, name_str, data_atual))
                    stats["sucesso_rpa"] += 1
                    report.registrar_baixado(nome_paciente)
                    report.registrar_metodo(download_method or "desconhecido")
                    report.atualizar_decisao_exame(  # [MÉTRICAS] coletado com sucesso
                        paciente=nome_paciente, data_exame=date_text,
                        nome_exame=name_str, decisao="baixado",
                        metodo=download_method or "desconhecido")
                    print(f"    [RPA] PDF salvo em {final_dest_path} [método: {download_method}]")
                else:
                    stats["falhas"] += 1
                    report.registrar_erro(
                        paciente=nome_paciente, cpf=cpf_paciente,
                        data_exame=date_text, nome_exame=name_str,
                        motivo=motivo_falha_extracao or "Não foi possível extrair o PDF do portal após todas as estratégias.",
                        etapa="extract_pdf_failed",
                    )
                    report.atualizar_decisao_exame(  # [MÉTRICAS]
                        paciente=nome_paciente, data_exame=date_text,
                        nome_exame=name_str, decisao="falha_extracao")

            except Exception as e:
                stats["falhas"] += 1
                print(f"ERRO: Erro crítico no exame {name_str}: {e}")
                report.registrar_erro(
                    paciente=nome_paciente, cpf=cpf_paciente,
                    data_exame=date_text, nome_exame=name_str,
                    motivo=f"Exceção não tratada no processamento do exame: {e}",
                    etapa="exam_crash",
                )
                report.atualizar_decisao_exame(  # [MÉTRICAS]
                    paciente=nome_paciente, data_exame=date_text,
                    nome_exame=name_str, decisao="exam_crash")

        if stats["falhas"] > 0 and stats["status"] == "success":
            stats["status"] = "partial_fail"

        report.registrar_paciente_processado()
        return stats

    except Exception as e:
        print(f"ERRO: Erro crítico no processamento do paciente: {e}")
        stats["status"] = "crash"
        stats["falhas"] += 1
        report.registrar_erro(
            paciente=nome_paciente, cpf=cpf_paciente,
            data_exame="", nome_exame="",
            motivo=f"Exceção crítica durante a sessão do paciente: {e}",
            etapa="patient_session_crash",
        )
        return stats
