"""
core/validation.py — dekorator `validate_body` walidujący ciało żądania Pydanticiem
(Phase 3, REFACTOR-03).

Dzielony przez auth/suppliers/warehouse blueprinty (obecnie: auth). Kształt błędu
({"error": "..."}, 400) jest zgodny z istniejącym `_validate_json_body` w app.py,
by frontendowy JS parsujący `.error` nie wymagał zmian.
"""
from __future__ import annotations

from functools import wraps

from flask import jsonify, request
from pydantic import BaseModel, ValidationError


def validate_body(model: type[BaseModel]):
    """Parsuje request.get_json() do `model`; wstrzykuje sparsowany model jako
    pierwszy argument pozycyjny widoku. Na ValidationError zwraca 400 z komunikatem
    dla pierwszego niepoprawnego pola i NIE wywołuje owiniętej funkcji."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            try:
                data = model.model_validate(request.get_json(silent=True) or {})
            except ValidationError as e:
                first = e.errors()[0]
                field = ".".join(str(p) for p in first["loc"])
                return jsonify({"error": f"Pole '{field}': {first['msg']}"}), 400
            return f(data, *args, **kwargs)
        return wrapped
    return decorator
