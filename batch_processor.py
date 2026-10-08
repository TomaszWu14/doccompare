"""
batch_processor.py — przetwarzanie wielu par dokumentów naraz.

Funkcje:
  auto_pair_files(filenames)   → lista par (a, b)
  run_batch(pairs, ...)        → generator wyników
  build_batch_excel(results)   → bytes Excel ze zbiorczym raportem
"""

import os
import re
import json
import time
from typing import Generator


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-PAROWANIE PLIKÓW
# ─────────────────────────────────────────────────────────────────────────────

# Wzorce par dokumentów (A_pattern → B_pattern)
PAIR_PATTERNS = [
    # PO vs PI: PO_4500000185 ↔ PI_4500000185
    (r'(?i)^PO[_\-\s]?(\d+)', r'(?i)^PI[_\-\s]?\1'),
    (r'(?i)^PI[_\-\s]?(\d+)', r'(?i)^PO[_\-\s]?\1'),
    # SAD vs CI: SAD_123 ↔ CI_123 lub MSNU8714046_SAD ↔ MSNU8714046_CI
    (r'(?i)^SAD[_\-\s]?(\w+)',  r'(?i)^CI[_\-\s]?\1'),
    (r'(?i)^(\w+)[_\-\s]SAD',  r'(?i)^\1[_\-\s]CI'),
    # CI vs PL: CI_123 ↔ PL_123
    (r'(?i)^CI[_\-\s]?(\w+)', r'(?i)^PL[_\-\s]?\1'),
    # FV vs WZ
    (r'(?i)^FV[_\-\s]?(\w+)', r'(?i)^WZ[_\-\s]?\1'),
    # Numer kontenera: MSNU8714046_*.pdf ↔ MSNU8714046_*.pdf
    (r'(?i)^([A-Z]{4}\d{7})[_\-\s].*_?PO',  r'(?i)^\1[_\-\s].*_?PI'),
    (r'(?i)^([A-Z]{4}\d{7})[_\-\s].*_?SAD', r'(?i)^\1[_\-\s].*_?CI'),
]

# Priorytety par (jakie typy dokumentów do siebie pasują)
DOC_TYPE_PAIRS = {
    'PO': ['PI', 'CI', 'PL'],
    'PI': ['PO', 'CI'],
    'CI': ['PO', 'PI', 'PL', 'SAD'],
    'SAD': ['CI', 'MULTI', 'PL'],
    'FV': ['WZ'],
    'WZ': ['FV'],
}


def detect_doc_type_from_name(filename: str) -> str:
    """Wykrywa typ dokumentu z nazwy pliku."""
    name = os.path.splitext(filename)[0].upper()
    for code in ['PO', 'PI', 'CI', 'PL', 'SAD', 'BL', 'WZ', 'FV', 'CMR', 'MULTI']:
        if re.search(rf'(^|[_\-\s]){code}([_\-\s]|$)', name):
            return code
    return 'auto'


def auto_pair_files(filenames: list[str]) -> tuple[list[dict], list[str]]:
    """
    Automatycznie paruje pliki PDF.
    Zwraca listę: [{file_a, file_b, type_a, type_b, method, confidence}]
    """
    pdf_files = [f for f in filenames if f.lower().endswith('.pdf')]
    paired = set()
    pairs = []

    def score_pair(a, b):
        """Oblicza podobieństwo nazw jako % wspólnych tokenów."""
        ta = set(re.split(r'[_\-\s\.]+', os.path.splitext(a)[0].lower()))
        tb = set(re.split(r'[_\-\s\.]+', os.path.splitext(b)[0].lower()))
        # Zostaw dyskryminator a/b w zbiorach: dwa pliki o tej samej roli
        # (np. oba "_A") nie powinny dostać sztucznego wyniku 1.0.
        ta -= {'pdf', ''}
        tb -= {'pdf', ''}
        if not ta or not tb:
            return 0
        intersection = ta & tb
        score = len(intersection) / max(len(ta), len(tb))
        # Kara za parowanie identycznej roli (oba 'a' albo oba 'b').
        if ('a' in ta and 'a' in tb) or ('b' in ta and 'b' in tb):
            score *= 0.5
        return score

    # 1. Parowanie przez wzorce nazw
    for i, fa in enumerate(pdf_files):
        if fa in paired:
            continue
        fa_base = os.path.splitext(fa)[0]
        for j, fb in enumerate(pdf_files):
            if i == j or fb in paired:
                continue
            fb_base = os.path.splitext(fb)[0]
            for pat_a, pat_b_template in PAIR_PATTERNS:
                m = re.search(pat_a, fa_base)
                if m:
                    num = m.group(1) if m.lastindex else ''
                    if num:
                        pat_b = pat_b_template.replace(r'\1', re.escape(num))
                    else:
                        # Brak grupy do podstawienia — usuń placeholder \1,
                        # by nie zostawić nieprawidłowego backreference w pat_b.
                        pat_b = pat_b_template.replace(r'\1', '')
                    if re.search(pat_b, fb_base):
                        type_a = detect_doc_type_from_name(fa)
                        type_b = detect_doc_type_from_name(fb)
                        pairs.append({
                            "file_a": fa, "file_b": fb,
                            "type_a": type_a, "type_b": type_b,
                            "method": "pattern", "confidence": 0.9
                        })
                        paired.add(fa); paired.add(fb)
                        break
            if fa in paired:
                break

    # 2. Parowanie przez typ dokumentu
    remaining = [f for f in pdf_files if f not in paired]
    by_type = {}
    for f in remaining:
        t = detect_doc_type_from_name(f)
        by_type.setdefault(t, []).append(f)

    for type_a, files_a in by_type.items():
        partner_types = DOC_TYPE_PAIRS.get(type_a, [])
        for type_b in partner_types:
            files_b = by_type.get(type_b, [])
            for fa in list(files_a):
                if fa in paired:
                    continue
                best_fb = None
                best_score = 0
                for fb in files_b:
                    if fb in paired:
                        continue
                    s = score_pair(fa, fb)
                    if s > best_score:
                        best_score = s
                        best_fb = fb
                if best_fb and best_score > 0.3:
                    pairs.append({
                        "file_a": fa, "file_b": best_fb,
                        "type_a": type_a, "type_b": type_b,
                        "method": "doctype", "confidence": best_score
                    })
                    paired.add(fa); paired.add(best_fb)

    # 3. Parowanie przez podobieństwo nazw (fallback)
    remaining = [f for f in pdf_files if f not in paired]
    while len(remaining) >= 2:
        fa = remaining[0]
        best_fb = None; best_score = 0
        for fb in remaining[1:]:
            s = score_pair(fa, fb)
            if s > best_score:
                best_score = s; best_fb = fb
        if best_fb and best_score > 0.4:
            pairs.append({
                "file_a": fa, "file_b": best_fb,
                "type_a": detect_doc_type_from_name(fa),
                "type_b": detect_doc_type_from_name(best_fb),
                "method": "similarity", "confidence": round(best_score, 2)
            })
            remaining.remove(fa); remaining.remove(best_fb)
        else:
            break

    # Niepasowane pliki
    all_paired = {p["file_a"] for p in pairs} | {p["file_b"] for p in pairs}
    unpaired = [f for f in pdf_files if f not in all_paired]

    return pairs, unpaired


# ─────────────────────────────────────────────────────────────────────────────
# BATCH RUNNER
# ─────────────────────────────────────────────────────────────────────────────

def run_batch(pairs: list[dict], upload_dir: str,
              uid: int, use_ai: bool = False) -> Generator[dict, None, None]:
    """
    Przetwarza pary dokumentów jeden po drugim.
    Yields: słownik z wynikiem każdej pary.
    """
    from comparator import extract_pdf_text, compare_documents

    for i, pair in enumerate(pairs):
        result = {
            "index": i,
            "total": len(pairs),
            "file_a": pair["file_a"],
            "file_b": pair["file_b"],
            "type_a": pair.get("type_a", "auto"),
            "type_b": pair.get("type_b", "auto"),
            "confidence": pair.get("confidence", 0),
            "method": pair.get("method", "manual"),
            "status": "processing",
            "error": None,
            "report": None,
            "elapsed_ms": 0,
        }

        t0 = time.time()
        from werkzeug.utils import secure_filename as _sf

        def _resolve(prefix, name):
            # Próbuj nazwy zabezpieczonej i surowej — secure_filename mógł zmienić
            # nazwę (spacje/polskie znaki) przy zapisie, więc sama wersja _sf nie trafia.
            cands = [os.path.join(upload_dir, f"{uid}_{prefix}_{_sf(name)}"),
                     os.path.join(upload_dir, f"{uid}_{prefix}_{name}")]
            for c in cands:
                if os.path.exists(c):
                    return c
            return cands[0]

        path_a = _resolve("b_a", pair['file_a'])
        path_b = _resolve("b_b", pair['file_b'])

        try:
            # Regex
            text_a = extract_pdf_text(path_a)
            text_b = extract_pdf_text(path_b)
            regex_result = compare_documents(text_a, text_b, "auto").to_dict()

            # Tabele
            try:
                from table_extractor import compare_tables
                tbl_result = compare_tables(path_a, path_b).to_dict()
            except Exception as e:
                tbl_result = {"ok": False, "error": str(e)[:200]}

            # Literówki
            try:
                from typo_detector import analyze_full_texts
                typo_result = analyze_full_texts(text_a, text_b,
                                                  pair["file_a"], pair["file_b"]).to_dict()
            except Exception as e:
                typo_result = {"ok": False, "error": str(e)[:200]}

            total_errors = ((regex_result.get("diff_count") or 0) +
                           (tbl_result.get("diff_count") or 0) +
                           (typo_result.get("error_count") or 0))
            total_warnings = ((regex_result.get("warn_count") or 0) +
                             (tbl_result.get("warn_count") or 0) +
                             (typo_result.get("warning_count") or 0))

            report = {
                "file_a": pair["file_a"],
                "file_b": pair["file_b"],
                "doc_type_a": pair.get("type_a", "auto"),
                "doc_type_b": pair.get("type_b", "auto"),
                "modules": {
                    "regex": {"ok": True, **regex_result},
                    "table": {"ok": True, **tbl_result} if tbl_result.get("ok") != False else tbl_result,
                    "typo":  {"ok": True, **typo_result} if typo_result.get("ok") != False else typo_result,
                },
                "total_errors": total_errors,
                "total_warnings": total_warnings,
                "risk_level": ("blad" if total_errors > 0 else
                               "ostrzezenie" if total_warnings > 0 else "ok"),
            }

            result["report"] = report
            result["status"] = "done"
            result["total_errors"] = total_errors
            result["total_warnings"] = total_warnings
            result["risk_level"] = report["risk_level"]

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)[:200]
        finally:
            # Sprzątaj pliki tymczasowe ZAWSZE (wcześniej tylko przy wyjątku —
            # przy sukcesie zalegały w upload_dir).
            for _p in (path_a, path_b):
                try:
                    if os.path.exists(_p):
                        os.remove(_p)
                except OSError:
                    pass

        result["elapsed_ms"] = int((time.time() - t0) * 1000)
        yield result


# ─────────────────────────────────────────────────────────────────────────────
# ZBIORCZY EXCEL
# ─────────────────────────────────────────────────────────────────────────────

def build_batch_excel(results: list[dict]) -> bytes:
    """Generuje zbiorczy Excel ze wszystkich par."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        from datetime import datetime
    except ImportError:
        raise ImportError("openpyxl nie jest zainstalowany")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Wyniki batch'
    ws.sheet_view.showGridLines = False

    def fill(h): return PatternFill('solid', fgColor=h.lstrip('#'))
    def font(bold=False, color='1a1d26', size=10):
        return Font(name='Calibri', bold=bold, color=color, size=size)
    def border():
        s = Side(style='thin', color='D0D4E0')
        return Border(left=s, right=s, top=s, bottom=s)

    # Nagłówek
    ws.merge_cells('A1:K1')
    ws['A1'] = f'DocCompare — Raport Batch | {datetime.now().strftime("%d.%m.%Y %H:%M")} | {len(results)} par'
    ws['A1'].font = Font(name='Calibri', bold=True, size=13, color='4F8EF7')
    ws['A1'].fill = fill('1e2130')
    ws['A1'].alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[1].height = 26

    # Kolumny
    headers = ['#', 'Dokument A', 'Dokument B', 'Typ A', 'Typ B',
               'Status', 'Błędy', 'Ostrzeżenia', 'Metoda', 'Pewność', 'Czas (ms)']
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=2, column=c, value=h)
        cell.font = Font(name='Calibri', bold=True, color='FFFFFF')
        cell.fill = fill('2a3050')
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=False)
        cell.border = border()
    ws.row_dimensions[2].height = 18
    ws.freeze_panes = 'A3'

    status_fills = {
        'ok': fill('e8faf0'), 'ostrzezenie': fill('fef3e2'), 'blad': fill('fde8e8'),
        'error': fill('fde8e8')
    }
    status_labels = {'ok': '✅ OK', 'ostrzezenie': '⚠ Ostrzeżenia',
                     'blad': '❌ Błędy', 'error': '💥 Błąd przetwarzania'}

    for r, res in enumerate(results, 3):
        st = res.get('risk_level') or res.get('status', 'error')
        row_fill = status_fills.get(st, fill('f8f9fc'))
        vals = [
            r - 2,
            res.get('file_a', ''),
            res.get('file_b', ''),
            res.get('type_a', 'auto'),
            res.get('type_b', 'auto'),
            status_labels.get(st, st),
            res.get('total_errors', '—'),
            res.get('total_warnings', '—'),
            res.get('method', ''),
            f"{int((res.get('confidence') or 0)*100)}%",
            res.get('elapsed_ms', ''),
        ]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = font()
            cell.fill = row_fill
            cell.border = border()
            cell.alignment = Alignment(horizontal='left', vertical='top', wrap_text=False)

    # Auto width
    col_widths = [4, 40, 40, 8, 8, 16, 7, 12, 12, 8, 10]
    for c, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = w

    import io
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
