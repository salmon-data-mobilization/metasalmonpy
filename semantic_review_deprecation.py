"""Deprecation of the in-package model call (hub item B-327, the mirror of B-326).

Brett ruled on 2026-09-25 (hub Q67) that the model call leaves metasalmon and
metasalmonpy: judgement runs in the user's harness against a review packet, and
the package reads the answers back. The in-package call -- ``llm_assess=True``,
the eleven ``llm_*`` arguments and :func:`~metasalmonpy.chat_decomposition` --
keeps working through the 0.6.x releases and warns once per top-level call that
it is deprecated, naming the removal release and the replacement. The removal
is the breaking release the S16 card sequences after at least one tagged 0.6.x
(hub items B-329 and B-330). The rules are section 6 of metasalmon's S16
execplan, and metasalmon's ``R/semantic-review-deprecation.R`` states them for R:

* The warning fires on :func:`~metasalmonpy.suggest_semantics`,
  :func:`~metasalmonpy.infer_dictionary`,
  :func:`~metasalmonpy.infer_salmon_datapackage_artifacts` and
  :func:`~metasalmonpy.create_sdp` whenever an ``llm_*`` argument holds a value
  other than its default, and on every call to
  :func:`~metasalmonpy.chat_decomposition`. **How it is detected is the one
  idiom difference** (PARITY.md row 65): R asks ``missing()``, so
  ``llm_assess = FALSE`` passed explicitly warns there; Python cannot tell an
  omitted argument from one passed with its default value, so a value equal to
  the default does not warn here.
* **Exactly one warning per top-level call.** The four entry points nest
  (``create_sdp()`` calls ``infer_salmon_datapackage_artifacts()``, which calls
  ``infer_dictionary()`` and ``suggest_semantics()``), and every inner call is
  handed the ``llm_*`` arguments, so each entry point enters a scope and only
  the outermost may warn.
* Emitted **after** the existing opt-in warnings ("LLM context is ignored unless
  llm_assess=True"), which fire inside the call: the outermost scope warns when
  it closes. ``chat_decomposition()`` warns on entry instead, before a possibly
  long interactive session. The opt-in contract itself is untouched: context
  supplied without ``llm_assess`` still warns that it is ignored and makes no
  call.
* The warning is :class:`LLMDeprecationWarning`, a :class:`FutureWarning`
  subclass so an end user sees it by default (a :class:`DeprecationWarning` is
  hidden outside ``__main__``), and so it can be filtered by category. The
  suite-wide quiet switch R keeps as the option
  ``metasalmon.llm_deprecation_quiet`` is a warnings filter here, installed by
  ``tests/conftest.py``; the tests that assert the warning switch it back on
  with ``pytest.warns`` or ``simplefilter("always")``.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import warnings
from typing import Callable

__all__ = ["LLMDeprecationWarning"]

#: The release that removes the in-package model call (decision 3 of the S16
#: execplan: the removal as 0.7.0, after at least one tagged 0.6.x).
LLM_DEPRECATION_REMOVAL_RELEASE = "0.7.0"

#: The eleven arguments of the in-package call, in metasalmon's
#: ``.ms_llm_arg_names()`` order.
LLM_ARGUMENT_NAMES = (
    "llm_assess",
    "llm_provider",
    "llm_model",
    "llm_api_key",
    "llm_base_url",
    "llm_reasoning_effort",
    "llm_top_n",
    "llm_context_files",
    "llm_context_text",
    "llm_timeout_seconds",
    "llm_request_fn",
)


class LLMDeprecationWarning(FutureWarning):
    """The in-package model call is deprecated and is removed in metasalmonpy 0.7.0.

    Raised as a warning by ``suggest_semantics(llm_assess=True)`` and the other
    entry points when an ``llm_*`` argument is used, and by every
    ``chat_decomposition()`` call. Model judgement now runs outside the package:
    write a review packet with :func:`~metasalmonpy.write_semantic_review_packet`,
    have your harness judge it, and read the answers back with
    :func:`~metasalmonpy.ingest_semantic_assessments`. Silence it until then with
    ``warnings.filterwarnings("ignore", category=metasalmonpy.LLMDeprecationWarning)``.
    """


_DEPTH = contextvars.ContextVar("metasalmonpy_llm_deprecation_depth", default=0)


def _message(entry: str) -> str:
    release = LLM_DEPRECATION_REMOVAL_RELEASE
    if entry == "chat_decomposition":
        surface = f"chat_decomposition() is deprecated and will be removed in metasalmonpy {release}."
    else:
        surface = (
            f"llm_assess=True and the llm_* arguments of {entry}() are deprecated and will be "
            f"removed in metasalmonpy {release}."
        )
    return (
        surface
        + " Model judgement now runs outside the package: write a review packet with "
        "write_semantic_review_packet(), have your harness judge it, and read the answers back "
        "with ingest_semantic_assessments(). Silence this warning until then with "
        "warnings.filterwarnings('ignore', category=metasalmonpy.LLMDeprecationWarning)."
    )


def _warn(entry: str, stacklevel: int) -> None:
    warnings.warn(_message(entry), LLMDeprecationWarning, stacklevel=stacklevel)


def _differs(value, default) -> bool:
    """Whether an argument holds something other than its default value."""
    if value is default:
        return False
    try:
        return not bool(value == default)
    except Exception:  # an array-like has no single truth value: it was supplied
        return True


def _llm_arguments_used(signature: inspect.Signature, args, kwargs) -> bool:
    """The Python reading of R's ``missing()`` test: any ``llm_*`` argument
    bound to a value other than its default."""
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError:
        return False  # the call itself will raise its own TypeError
    for name in LLM_ARGUMENT_NAMES:
        if name not in bound.arguments or name not in signature.parameters:
            continue
        if _differs(bound.arguments[name], signature.parameters[name].default):
            return True
    return False


def deprecated_llm_entry_point(entry: str) -> Callable:
    """Decorate one of the four ``llm_*`` entry points.

    Only the outermost of a nest of entry points warns, when it returns or
    raises, and only when its own call used an ``llm_*`` argument.
    """

    def decorator(function: Callable) -> Callable:
        signature = inspect.signature(function)

        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            depth = _DEPTH.get()
            token = _DEPTH.set(depth + 1)
            triggered = depth == 0 and _llm_arguments_used(signature, args, kwargs)
            try:
                return function(*args, **kwargs)
            finally:
                _DEPTH.reset(token)
                if triggered:
                    _warn(entry, stacklevel=3)

        return wrapper

    return decorator


def deprecated_on_every_call(entry: str) -> Callable:
    """Decorate :func:`~metasalmonpy.chat_decomposition`: warned on entry, every call.

    The scope keeps the ``suggest_semantics()`` call inside from warning again.
    """

    def decorator(function: Callable) -> Callable:
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            depth = _DEPTH.get()
            token = _DEPTH.set(depth + 1)
            try:
                if depth == 0:
                    _warn(entry, stacklevel=3)
                return function(*args, **kwargs)
            finally:
                _DEPTH.reset(token)

        return wrapper

    return decorator
