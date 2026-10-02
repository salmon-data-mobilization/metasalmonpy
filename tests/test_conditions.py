"""Package-origin conditions remain catchable through their old builtin types."""
import pytest
import requests

from metasalmonpy import github_io, ices_vocab, knb_environments, llm_review, metadata, sdp_schema

from metasalmonpy import conditions


@pytest.mark.parametrize("invoke,family,builtin,message", [
    (lambda: sdp_schema.set_sdp_schema_source("invalid"), "Validation", ValueError, "source must be one of"),
    (lambda: llm_review.validate_context_files(3), "Llm", TypeError, "local file paths"),
    (lambda: knb_environments.knb_config("invalid"), "Publication", ValueError, "Unknown KNB environment"),
    (lambda: ices_vocab.ices_codes(""), "Retrieval", ValueError, "code_type must be"),
    (lambda: metadata.ensure_resource_mapping({}), "", ValueError, "resources cannot be empty"),
])
def test_selective_package_catch_preserves_builtin_catch(invoke, family, builtin, message):
    expected = getattr(conditions, "Metasalmon" + family + "Error")
    with pytest.raises(expected, match=message) as caught:
        invoke()
    assert isinstance(caught.value, builtin)
    assert isinstance(caught.value, conditions.MetasalmonError)
    for other in ("Validation", "Llm", "Publication", "Retrieval"):
        if other != family:
            assert not isinstance(caught.value, getattr(conditions, "Metasalmon" + other + "Error"))


def test_dependency_exception_is_rethrown_unchanged(monkeypatch):
    error = requests.HTTPError("upstream unavailable")
    class Response:
        status_code = 503
        headers = {}
        def raise_for_status(self):
            raise error
    monkeypatch.setattr(github_io.requests, "get", lambda *a, **k: Response())
    with pytest.raises(requests.HTTPError) as caught:
        github_io.ms_setup_github(repo="org/repo", token="fake-test-token")
    assert caught.value is error
    assert not isinstance(error, conditions.MetasalmonError)


def test_existing_specialized_errors_keep_names_messages_and_metadata():
    from metasalmonpy import knb_publication, reproducibility, sdp_methods
    cases = [
        (knb_publication.KnbHttpError("denied", status=403), conditions.MetasalmonPublicationError, RuntimeError),
        (sdp_schema.SdpSchemaError("bad bundle"), conditions.MetasalmonValidationError, RuntimeError),
        (sdp_methods.SdpExtensionError("bad extension"), conditions.MetasalmonValidationError, ValueError),
        (reproducibility.ReproducibilityManifestError("bad manifest"), conditions.MetasalmonValidationError, ValueError),
    ]
    assert cases[0][0].status == 403
    for error, family, builtin in cases:
        assert isinstance(error, family)
        assert isinstance(error, builtin)
        assert str(error) == error.args[0]


def test_structural_validation_error_preserves_the_issues_frame():
    import pandas as pd
    from metasalmonpy import package_io
    issues = pd.DataFrame({"message": ["a structural issue"]})
    with pytest.raises(conditions.MetasalmonError) as caught:
        package_io._abort_package_validation_issues(issues)
    assert isinstance(caught.value, ValueError)
    assert caught.value.issues is issues


_CONCRETE = [cls for name, cls in vars(conditions).items()
             if name.startswith("_") and isinstance(cls, type)
             and issubclass(cls, conditions.MetasalmonCondition)]


@pytest.mark.parametrize("cls", _CONCRETE, ids=lambda cls: cls.__name__)
def test_builtin_constructors_messages_and_pickle_survive(cls):
    import pickle
    builtin = cls.__bases__[0]
    args = (2, "missing", "example.csv") if issubclass(builtin, OSError) else ("literal {message}",)
    original = builtin(*args)
    error = cls(*args)
    assert isinstance(error, builtin)
    assert str(error) == str(original)
    assert error.args == original.args
    if isinstance(error, OSError):
        assert (error.errno, error.strerror, error.filename) == (
            original.errno, original.strerror, original.filename)
    restored = pickle.loads(pickle.dumps(error))
    assert type(restored) is cls
    assert str(restored) == str(error)
    if issubclass(cls, conditions.MetasalmonWarning):
        assert not isinstance(error, conditions.MetasalmonError)
        with pytest.warns(builtin) as recorded:
            import warnings
            warnings.warn("literal {message}", category=cls)
        assert isinstance(recorded[0].message, conditions.MetasalmonWarning)
    else:
        assert not isinstance(error, conditions.MetasalmonWarning)


def test_real_bioportal_warning_retains_runtime_filter_and_family(monkeypatch):
    from metasalmonpy import term_search
    monkeypatch.delenv("BIOPORTAL_APIKEY", raising=False)
    monkeypatch.setattr(term_search, "_warned_bioportal_missing", False)
    with pytest.warns(RuntimeWarning, match="BioPortal API key missing") as recorded:
        term_search._search_bioportal("salmon", "entity")
    assert isinstance(recorded[0].message, conditions.MetasalmonRetrievalWarning)


def test_real_context_warning_retains_user_filter_and_family(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    with pytest.warns(UserWarning, match="Skipping empty context file") as recorded:
        assert llm_review._read_context_file(empty) is None
    assert isinstance(recorded[0].message, conditions.MetasalmonLlmWarning)


def test_owned_constructor_and_warning_inventory_has_no_unclassed_sites():
    """Namespace-backed AST reach covers emissions, stored errors and subclasses.

    ImportError dependency guards keep the original type and cause; retire that
    exception when the corresponding optional/core dependency guard disappears.
    The YAML ConstructorError retains the loader's special marked-error type.
    Bare/variable rethrows preserve the original external exception identity.
    """
    import ast
    import importlib
    from pathlib import Path
    package_root = Path(conditions.__file__).parent
    builtin_errors = {"ValueError", "TypeError", "RuntimeError", "FileNotFoundError",
                      "FileExistsError", "PermissionError", "NotADirectoryError",
                      "NotImplementedError", "AttributeError", "KeyError", "ImportError"}
    domains = {
        "Validation": {"dictionary", "sdp_schema", "validation", "sssom",
                       "measurement_decompositions", "reproducibility",
                       "observation_structures", "sdp_methods", "semantic_closure"},
        "Llm": {"llm_review", "chat_decomposition"},
        "Publication": {"knb_archive", "knb_environments", "knb_publication",
                        "eml", "edh_xml", "dwc_dp", "dwc_dp_export", "github_io"},
        "Retrieval": {"term_search", "term_search_smn", "ontology_fetch", "ices_vocab"},
    }
    emissions = []
    for path in package_root.glob("*.py"):
        if path.name in {"conditions.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text())
        module = importlib.import_module("metasalmonpy." + path.stem)
        domain = next((family for family, names in domains.items() if path.stem in names), "")
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "conditions":
                for alias in node.names:
                    concrete = getattr(conditions, alias.name)
                    severity = "Warning" if issubclass(concrete, conditions.MetasalmonWarning) else "Error"
                    assert issubclass(concrete, getattr(conditions, "Metasalmon" + domain + severity)), (
                        path.name, alias.name, domain)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                name = node.func.id
                # The ImportError exclusion is narrow and documented above.
                assert name not in builtin_errors - {"ImportError"}, (path.name, node.lineno, name)
                emitted = getattr(module, name, None)
                if isinstance(emitted, type) and issubclass(emitted, conditions.MetasalmonError):
                    emissions.append((path.stem, name))
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "warnings.warn":
                category = next((key.value for key in node.keywords if key.arg == "category"),
                                node.args[1] if len(node.args) > 1 else None)
                assert isinstance(category, ast.Name), (path.name, node.lineno)
                assert issubclass(getattr(module, category.id), conditions.MetasalmonWarning)
    # Positive controls establish that the scan reaches a stored error and a
    # builtin-replacing constructor from distinct modules.
    assert ("package_io", "_ValueError") in emissions
    assert ("sdp_schema", "_ValueError") in emissions
