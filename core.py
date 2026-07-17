import os
import time
import re
import shutil
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
    SITE_URL, USER, PASS, DOWNLOAD_DIR, SEARCH_BAR_SELECTOR,
    EXAM_YEAR_CUTOFF, EXAM_YEAR_MAX, EXAM_EXCLUDE_KEYWORDS, EXAM_REPORT_EXCLUDE_MARKERS,
    SEARCH_TIMEOUT, SEARCH_NOT_FOUND_GRACE, STUDY_WAIT_TIMEOUT,
    LAUDO_WAIT_TIMEOUT, REPORT_POPUP_TIMEOUT,
)


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


def _texto_indica_skip(texto: str) -> bool:
    markers = [remove_accents(m).upper() for m in EXAM_REPORT_EXCLUDE_MARKERS]
    blob = remove_accents(texto).upper()
    return any(marker in blob for marker in markers)


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
        gc = gspread.service_account(filename="credenciais.json")
        planilha = gc.open_by_url(sheet_url).sheet1
        df = pd.DataFrame(planilha.get_all_records())
        pacientes_filtrados = [{"sheet_row": index + 2, "nome": str(row.get("Por gentileza, informe o seu nome completo:", "")).strip(), "cpf": str(row.get("CPF", "")).strip()}
                               for index, row in df.iterrows() if str(row.get("STATUS", "0")) == "1"]
        return {"pacientes": pacientes_filtrados}
    except Exception as e:
        return {"error": str(e)}


def update_sheet_status(sheet_url, row_index, status_value):
    try:
        gc = gspread.service_account(filename="credenciais.json")
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
    while time.time() < deadline:
        linhas = _ler_linhas_resultado(page)
        if linhas:
            viu_linhas = True
            for idx, (nome_txt, id_txt) in enumerate(linhas):
                if normalize_name(nome_txt) == alvo:
                    return {"index": idx, "id_cell": id_txt.strip(), "viu_linhas": True}
        # Saída rápida: já deu tempo da busca rodar e o badge mostra 0 com
        # a tabela vazia -> paciente não existe (evita esperar o timeout todo).
        elif (time.time() - inicio) > grace and _badge_resultado(page) == 0:
            return {"index": None, "id_cell": None, "viu_linhas": viu_linhas}
        time.sleep(0.25)

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
                    print(f"    [RPA] Ignorado (laudo de localização pré-operatória): {name_str}")
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
                    with context.expect_page(timeout=15000) as np:
                        page.locator("span[id$='_printBtn'].btnPrint").click(force=True)
                    new_page = np.value
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
