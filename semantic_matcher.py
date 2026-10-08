"""
semantic_matcher.py — dopasowanie semantyczne pozycji towarowych.

Rozwiązuje problem:
  'NL753-S-40' vs 'NL753S40' → ten sam produkt
  'Extension tube 150cm' vs 'Przedłużacz do pompy 150cm' → prawdopodobnie ten sam
  'SANVIFLON I.V. CANNULA 22G' vs 'Kaniula dożylna 22G' → wysoka szansa że to samo
"""

import re
from typing import Optional
from normalizer import normalize_ref, refs_match

try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


import threading as _threading
import logging as _log

# FIX 9: Use a distinct sentinel object (not False) so None means "uninitialised",
# _SBERT_FAILED means "tried and failed", and any other value is the loaded model.
_SBERT_FAILED = object()   # sentinel for load failure — clearer than False
_sbert_model = None        # None = not yet attempted
_sbert_lock  = _threading.Lock()

def _get_sbert():
    """Lazy SentenceTransformer singleton (multilingual MiniLM).

    Returns the loaded model, or None if unavailable.
    _sbert_model is None before first call, the model on success,
    or _SBERT_FAILED on failure.
    """
    global _sbert_model
    # Fast path: already initialised (model or failed sentinel)
    if _sbert_model is not None:
        return None if _sbert_model is _SBERT_FAILED else _sbert_model
    with _sbert_lock:
        # Re-check inside the lock (double-checked locking)
        if _sbert_model is not None:
            return None if _sbert_model is _SBERT_FAILED else _sbert_model
        try:
            from sentence_transformers import SentenceTransformer
            _sbert_model = SentenceTransformer(
                "paraphrase-multilingual-MiniLM-L12-v2")
            _log.getLogger(__name__).info(
                "SentenceTransformer (multilingual-MiniLM) loaded")
        except Exception as exc:
            _log.getLogger(__name__).debug(
                "sentence-transformers unavailable: %s", exc)
            _sbert_model = _SBERT_FAILED  # FIX 9: use sentinel, not False
    return None if _sbert_model is _SBERT_FAILED else _sbert_model


# ─── SŁOWNIK MEDYCZNY PL/EN ───────────────────────────────────────────────────
# Kluczowe terminy z branży medical devices

MEDICAL_DICT_EN_PL = {
    # Kaniule, igły
    'cannula': 'kaniula', 'cannulae': 'kaniula', 'i.v. cannula': 'kaniula dożylna',
    'needle': 'igła', 'hypodermic': 'podskórny',
    # Strzykawki
    'syringe': 'strzykawka', 'plunger': 'tłoczek',
    # Cewniki
    'catheter': 'cewnik', 'foley': 'cewnik foleya',
    'urinary catheter': 'cewnik urologiczny',
    # Opatrunki
    'bandage': 'bandaż', 'gauze': 'gaza', 'compress': 'kompress',
    'wound': 'rana', 'dressing': 'opatrunek',
    'nonwoven': 'włóknina', 'non-woven': 'włóknina',
    # Rękawice
    'gloves': 'rękawice', 'latex': 'lateksowy', 'nitrile': 'nitrylowy',
    # Sprzęt infuzyjny
    'infusion': 'infuzja', 'drip': 'kroplówka', 'iv set': 'zestaw do kroplówki',
    'extension tube': 'przedłużacz', 'giving set': 'zestaw infuzyjny',
    # Nerki, baseny
    'kidney dish': 'nerka', 'basin': 'miednica', 'bedpan': 'basen',
    # Inne
    'sterile': 'jałowy', 'non-sterile': 'niejałowy',
    'disposable': 'jednorazowy', 'single use': 'jednorazowy',
    'pvc': 'pvc', 'silicone': 'silikonowy',
    'french': 'fr', 'gauge': 'g',
}

MEDICAL_DICT_PL_EN = {v: k for k, v in MEDICAL_DICT_EN_PL.items()}


def normalize_product_description(text: str) -> str:
    """Normalizuje opis produktu — usuwa stopwords, normalizuje jednostki."""
    if not text:
        return ''
    t = text.lower().strip()
    # FIX 10: Replace hyphens with spaces before removing punctuation so that
    # "non-woven" → "non woven" and similar compound words are kept matchable.
    t = t.replace('-', ' ')
    # Normalizuj jednostki
    t = re.sub(r'\b(\d+)\s*cm\b', r'\1cm', t)
    t = re.sub(r'\b(\d+)\s*mm\b', r'\1mm', t)
    t = re.sub(r'\b(\d+)\s*ml\b', r'\1ml', t)
    t = re.sub(r'\b(\d+)\s*g\b', r'\1g', t)
    t = re.sub(r'\b(\d+)\s*fr\b', r'\1fr', t)  # French size
    # Normalizuj rozmiary igieł/kaniuli
    t = re.sub(r'\b(\d+)\s*gauge\b', r'\1g', t)
    t = re.sub(r'\bi\.?v\.?\b', 'iv', t)
    # Usuń interpunkcję (zostaw liczby i litery)
    t = re.sub(r'[.,;:()\[\]]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def translate_to_common(text: str) -> str:
    """Tłumaczy terminy PL↔EN do wspólnej reprezentacji."""
    t = normalize_product_description(text)
    # Zamień polskie terminy na angielskie (jako bazę)
    for pl_term, en_term in MEDICAL_DICT_PL_EN.items():
        t = re.sub(r'\b' + re.escape(pl_term) + r'\b', en_term, t)
    return t




# ─── DOPASOWANIE OPISÓW PRODUKTÓW ────────────────────────────────────────────

class ProductMatcher:
    """
    Dopasowuje opisy produktów między dokumentami.
    Używa kombinacji:
    1. Normalizacji + exact match
    2. TF-IDF char n-gram similarity
    3. rapidfuzz token sort ratio
    4. SBERT multilingual embeddings + FAISS NN search (gdy dostępne)
    """

    def __init__(self):
        self._vectorizer = None
        self._corpus = []
        self._threshold = 0.55  # min similarity dla dopasowania
        self._faiss_index = None   # faiss.IndexFlatIP — fast cosine NN search
        self._faiss_embs  = None   # normalized float32 embeddings of corpus

    def fit(self, descriptions: list[str]):
        """Buduje model na podstawie corpus opisów."""
        # FIX 11: Guard against empty or all-blank corpus
        if not descriptions or all(not d for d in descriptions):
            return self  # nothing to fit
        self._corpus = [normalize_product_description(d) for d in descriptions]
        if HAS_SKLEARN and len(self._corpus) > 1:
            self._vectorizer = TfidfVectorizer(
                analyzer='char_wb',
                ngram_range=(2, 4),
                min_df=1,
                sublinear_tf=True,
            )
            try:
                self._vectorizer.fit(self._corpus)
            except Exception:
                self._vectorizer = None

        # ── SBERT embeddings + FAISS index ───────────────────────────────────
        model = _get_sbert()
        if model and self._corpus:
            try:
                import numpy as np
                embs = model.encode(
                    [translate_to_common(d) for d in descriptions],
                    show_progress_bar=False,
                    batch_size=32,
                    normalize_embeddings=True,   # L2-norm → IP == cosine
                ).astype(np.float32)
                self._faiss_embs = embs
                try:
                    import faiss
                    idx = faiss.IndexFlatIP(embs.shape[1])
                    idx.add(embs)
                    self._faiss_index = idx
                except ImportError:
                    self._faiss_index = None   # FAISS not installed; use embs only
            except Exception:
                pass
        return self

    def similarity(self, a: str, b: str) -> float:
        """Oblicza similarity między dwoma opisami (0-1)."""
        na = normalize_product_description(translate_to_common(a))
        nb = normalize_product_description(translate_to_common(b))

        if not na or not nb:
            return 0.0

        # Exact match po normalizacji
        if na == nb:
            return 1.0

        scores_sbert_mode = False
        sbert_score = 0.0
        model = _get_sbert()
        if model:
            try:
                import numpy as np
                embs = model.encode([na, nb], show_progress_bar=False,
                                    normalize_embeddings=True)
                sbert_score = float(np.dot(embs[0], embs[1]))
                sbert_score = max(0.0, sbert_score)
                scores_sbert_mode = True
            except Exception:
                pass

        # Track weighted_sum and total_weight separately so that missing
        # components (no TF-IDF when corpus has 1 item, no SBERT, etc.) are
        # compensated by normalization — the result is always in [0, 1] and
        # the threshold 0.55 remains meaningful regardless of what's available.
        weighted_sum  = 0.0
        total_weight  = 0.0

        if scores_sbert_mode:
            # SBERT is primary (weight 0.5); others supplement at 0.3 / 0.2
            weighted_sum += sbert_score * 0.5
            total_weight += 0.5
            tfidf_w, rz_w = 0.3, 0.2
        else:
            # No SBERT: TF-IDF 60% + rapidfuzz 40%
            tfidf_w, rz_w = 0.6, 0.4

        if HAS_SKLEARN and self._vectorizer:
            try:
                va = self._vectorizer.transform([na])
                vb = self._vectorizer.transform([nb])
                weighted_sum += cosine_similarity(va, vb)[0][0] * tfidf_w
                total_weight += tfidf_w
            except Exception:
                pass

        if HAS_RAPIDFUZZ:
            _pr = fuzz.partial_ratio(na, nb)
            # Kara za dużą różnicę długości: krótki opis będący podciągiem
            # dłuższego nie powinien dostać 100% (np. 'gauze' vs 'gauze swab
            # sterile 100x100'), bo latałby na nieswój, bardziej szczegółowy produkt.
            _lr = min(len(na), len(nb)) / max(len(na), len(nb), 1)
            # Kara długości musi działać spójnie: bierzemy lepszy z
            # token_sort_ratio / partial_ratio, ale DOPIERO POTEM mnożymy przez
            # _lr — inaczej token_sort_ratio omijał karę i krótki podciąg
            # dostawał pełną punktację.
            rz = max(fuzz.token_sort_ratio(na, nb), _pr) * _lr / 100.0
            weighted_sum += rz * rz_w
            total_weight += rz_w

        if total_weight <= 0:
            wa = set(na.split())
            wb = set(nb.split())
            if wa and wb:
                return len(wa & wb) / max(len(wa), len(wb))
            return 0.0

        return weighted_sum / total_weight

    def find_best_match(self, query: str, candidates: list[str],
                        threshold: float = None,
                        exclude: set = None) -> tuple[Optional[int], float]:
        """
        Finds the best unused match for query among candidates.
        exclude: set of candidate indices already claimed by other matches.
        Uses FAISS index if available (built during fit()), otherwise brute-force.
        """
        thr = self._threshold if threshold is None else threshold
        exclude = exclude or set()

        # ── Fast path: FAISS NN search ────────────────────────────────────
        if self._faiss_index is not None:
            model = _get_sbert()
            if model:
                try:
                    import numpy as np
                    nq = translate_to_common(query)
                    q_emb = model.encode([nq], show_progress_bar=False,
                                         normalize_embeddings=True).astype(np.float32)
                    # Fetch enough results so we can skip excluded indices
                    k = min(self._faiss_index.ntotal,
                            max(50, len(exclude) + 10))
                    D, I = self._faiss_index.search(q_emb, k)
                    best_idx_f, best_sim_f = None, 0.0
                    for score, idx in zip(D[0].tolist(), I[0].tolist()):
                        if idx < 0 or idx >= len(candidates) or idx in exclude:
                            continue
                        blended = self.similarity(query, candidates[idx])
                        if blended > best_sim_f:
                            best_sim_f = blended
                            best_idx_f = idx
                    if best_idx_f is not None and best_sim_f >= thr:
                        return int(best_idx_f), best_sim_f
                    # Below threshold or all FAISS results excluded — fall through to slow path
                except Exception:
                    pass   # fall through to slow path

        # ── Slow path: brute-force ────────────────────────────────────────
        best_idx, best_sim = None, 0.0
        for i, cand in enumerate(candidates):
            if i in exclude:
                continue
            sim = self.similarity(query, cand)
            if sim > best_sim:
                best_sim = sim
                best_idx = i
        if best_sim >= thr:
            return best_idx, best_sim
        return None, best_sim


def match_items_semantic(items_a: list[dict], items_b: list[dict],
                          ref_key: str = 'ref',
                          desc_key: str = 'description',
                          fuzzy_ref_threshold: int = 85,
                          desc_similarity_threshold: float = 0.55) -> list[dict]:
    """
    Dopasowuje pozycje towarowe między dwoma dokumentami.

    Algorytm:
    1. Exact match po REF (po normalizacji)
    2. Fuzzy match po REF (rapidfuzz)
    3. Semantic match po opisie produktu (gdy REF nie pasuje)
    4. Unmatched — pozycje bez pary

    Returns lista par z confidence i metodą dopasowania.
    """
    matched_a = set()
    matched_b = set()
    results = []

    # Faza 1: Exact REF match (po normalizacji)
    for i, item_a in enumerate(items_a):
        ref_a = str(item_a.get(ref_key, '') or '')
        for j, item_b in enumerate(items_b):
            if j in matched_b:
                continue
            ref_b = str(item_b.get(ref_key, '') or '')
            if normalize_ref(ref_a) == normalize_ref(ref_b) and normalize_ref(ref_a):
                results.append({
                    'item_a': item_a, 'item_b': item_b,
                    'match_type': 'ref_exact', 'confidence': 1.0,
                    'idx_a': i, 'idx_b': j,
                })
                matched_a.add(i); matched_b.add(j)
                break

    # Faza 2: Fuzzy REF match — globalnie „najlepsze pary najpierw".
    # Wcześniej zachłannie w kolejności A: pierwsze A zabierało swoje najlepsze B,
    # więc późniejsze A — będące lepszym dopasowaniem do tego samego B — nie mogło
    # go odzyskać (np. NL753S40 vs NL753S45). Teraz zbieramy wszystkie kandydujące
    # pary nad progiem i przydzielamy w kolejności malejącej pewności.
    fuzzy_candidates = []
    for i, item_a in enumerate(items_a):
        if i in matched_a:
            continue
        ref_a = str(item_a.get(ref_key, '') or '')
        for j, item_b in enumerate(items_b):
            if j in matched_b:
                continue
            ref_b = str(item_b.get(ref_key, '') or '')
            ok, conf = refs_match(ref_a, ref_b, fuzzy_ref_threshold)
            if ok:
                fuzzy_candidates.append((conf, i, j))
    fuzzy_candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    phase2 = []
    for conf, i, j in fuzzy_candidates:
        if i in matched_a or j in matched_b:
            continue
        phase2.append({
            'item_a': items_a[i], 'item_b': items_b[j],
            'match_type': 'ref_fuzzy', 'confidence': conf,
            'idx_a': i, 'idx_b': j,
        })
        matched_a.add(i); matched_b.add(j)
    phase2.sort(key=lambda r: r['idx_a'])   # zachowaj kolejność A w wynikach
    results.extend(phase2)

    # Faza 3: Semantic match po opisie
    unmatched_a = [i for i in range(len(items_a)) if i not in matched_a]
    unmatched_b = [j for j in range(len(items_b)) if j not in matched_b]

    if unmatched_a and unmatched_b:
        matcher = ProductMatcher()
        desc_b_list = [str(items_b[j].get(desc_key, '') or '') for j in unmatched_b]
        matcher.fit(desc_b_list)
        used_desc_idx: set[int] = set()

        for i in unmatched_a:
            desc_a = str(items_a[i].get(desc_key, '') or '')
            if not desc_a.strip():
                continue
            best_k, best_sim = matcher.find_best_match(
                desc_a, desc_b_list,
                threshold=desc_similarity_threshold,
                exclude=used_desc_idx,
            )
            if best_k is not None:
                j = unmatched_b[best_k]
                results.append({
                    'item_a': items_a[i], 'item_b': items_b[j],
                    'match_type': 'desc_semantic', 'confidence': best_sim,
                    'idx_a': i, 'idx_b': j,
                })
                matched_a.add(i); matched_b.add(j)
                used_desc_idx.add(best_k)

    # Faza 4: Unmatched
    for i in range(len(items_a)):
        if i not in matched_a:
            results.append({
                'item_a': items_a[i], 'item_b': None,
                'match_type': 'only_in_a', 'confidence': 0.0,
                'idx_a': i, 'idx_b': None,
            })
    for j in range(len(items_b)):
        if j not in matched_b:
            results.append({
                'item_a': None, 'item_b': items_b[j],
                'match_type': 'only_in_b', 'confidence': 0.0,
                'idx_a': None, 'idx_b': j,
            })

    return results
