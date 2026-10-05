"""An explicit reviewed sidecar selects one ledger; fallback stays unchanged."""

import builtins
import hashlib
import importlib.util
import shutil
from pathlib import Path

import pandas as pd
import pytest

import metasalmonpy as ms
from metasalmonpy import eml

REPORT = 'reproducibility/provenance/semantic-iri-dereference.csv'
ROOT_LEDGER = 'reviewed_semantic_selections.csv'
CANONICAL_LEDGER = 'reproducibility/reviewed_semantic_selections.csv'
DATA = Path(__file__).parent / 'data'
requires_yaml = pytest.mark.skipif(importlib.util.find_spec('yaml') is None,
                                  reason='Native mapped sidecar requires optional PyYAML/[eml].')


def core(root):
    target = root / 'metadata'
    target.mkdir(parents=True)
    pd.DataFrame({'dataset_id': ['ledger-authority']}).to_csv(target / 'dataset.csv', index=False)
    pd.DataFrame({'table_id': ['observations']}).to_csv(target / 'tables.csv', index=False)
    pd.DataFrame({'term_iri': [None]}).to_csv(target / 'column_dictionary.csv', index=False)


def ledger(root, relative, iri):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({'decision': ['accepted'], 'iri': [iri]}).to_csv(target, index=False)


def raw_inputs(root):
    return {path: path.read_bytes() for path in root.rglob('*')
            if path.is_file() and path != root / REPORT}


def assert_inputs(before):
    assert all(path.read_bytes() == raw for path, raw in before.items())


@requires_yaml
@pytest.mark.parametrize('selected', [ROOT_LEDGER, CANONICAL_LEDGER])
def test_native_valid_mapping_ignores_the_unused_accepted_ledger(tmp_path, selected):
    root = tmp_path / 'sdp'
    shutil.copytree(DATA / 'knb/sdp-full', root)
    mapping_path = root / 'metadata/eml-mapping.yml'
    mapping = eml._read_mapping_yaml(mapping_path)
    if selected == ROOT_LEDGER:
        (root / ROOT_LEDGER).write_bytes((root / CANONICAL_LEDGER).read_bytes())
        mapping_path.write_text(mapping_path.read_text().replace(CANONICAL_LEDGER, ROOT_LEDGER))
        mapping = eml._read_mapping_yaml(mapping_path)
    assert mapping['semantic_review']['path'] == selected
    assert hashlib.sha256((root / selected).read_bytes()).hexdigest() == mapping['semantic_review']['sha256']
    pkg = ms.validate_salmon_datapackage(root, require_iris=True)['package']
    assert eml._validate_mapping(mapping, pkg)
    native = eml._read_semantic_review(root, pkg, mapping)
    assert len(native) > 0
    unused = CANONICAL_LEDGER if selected == ROOT_LEDGER else ROOT_LEDGER
    stale = 'https://unused-ledger.invalid/stale#not-selected'
    ledger(root, unused, stale)
    assert eml._read_semantic_review(root, pkg, mapping).equals(native)
    before = raw_inputs(root)
    seen = []
    def request(iri):
        seen.append(iri)
        return {'status': 404 if iri == stale else 200, 'final_url': iri}
    rows = ms.verify_sdp_semantic_iris(root, requester=request)
    assert stale not in seen and stale not in rows.iri.tolist()
    assert set(native.iri).issubset(set(seen))
    assert seen == sorted(set(seen), key=lambda value: value.encode('utf-8'))
    assert list(rows) == ['iri', 'status', 'final_url', 'error', 'attempts']
    assert (rows.attempts == 1).all()
    assert_inputs(before)


@requires_yaml
@pytest.mark.parametrize('selected', [ROOT_LEDGER, CANONICAL_LEDGER])
def test_unselected_broken_csv_is_never_read(tmp_path, selected):
    core(tmp_path)
    iri = 'https://mapped.invalid/exact;variant#one'
    ledger(tmp_path, selected, iri)
    unused = CANONICAL_LEDGER if selected == ROOT_LEDGER else ROOT_LEDGER
    target = tmp_path / unused
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('decision,iri\n"unterminated')
    (tmp_path / 'metadata/eml-mapping.yml').write_text(f'semantic_review: {{path: {selected}}}\n')
    before = raw_inputs(tmp_path)
    seen = []
    rows = ms.verify_sdp_semantic_iris(tmp_path, requester=lambda value:
                                     (seen.append(value) or {'status': 200, 'final_url': value}))
    assert seen == [iri] and rows.iri.tolist() == [iri]
    assert_inputs(before)


@pytest.mark.parametrize('sidecar', [None, 'status: draft\n', 'semantic_review: [',
                                     '42\n', 'semantic_review: {path: other.csv}\n',
                                     'semantic_review: {path: [one, two]}\n'])
def test_unqualified_sidecar_preserves_the_existing_two_ledger_union(tmp_path, sidecar):
    if sidecar is not None and importlib.util.find_spec('yaml') is None:
        pytest.skip('Mapped sidecar parsing needs optional PyYAML.')
    core(tmp_path)
    first = 'https://fallback.invalid/root#one'
    second = 'https://fallback.invalid/canonical#two'
    ledger(tmp_path, ROOT_LEDGER, first)
    ledger(tmp_path, CANONICAL_LEDGER, second)
    if sidecar is not None:
        (tmp_path / 'metadata/eml-mapping.yml').write_text(sidecar)
    before = raw_inputs(tmp_path)
    seen = []
    rows = ms.verify_sdp_semantic_iris(tmp_path, requester=lambda iri:
                                     (seen.append(iri) or {'status': 200, 'final_url': iri}))
    assert seen == sorted([first, second], key=lambda value: value.encode('utf-8'))
    assert rows.iri.tolist() == seen
    assert_inputs(before)


@requires_yaml
@pytest.mark.parametrize('selected', [ROOT_LEDGER, CANONICAL_LEDGER])
@pytest.mark.parametrize('shape', ['singleton_sequence', 'padded_string'])
def test_native_invalid_path_shape_does_not_choose_a_ledger(tmp_path, selected, shape):
    core(tmp_path)
    ledger(tmp_path, ROOT_LEDGER, 'https://fallback.invalid/root')
    ledger(tmp_path, CANONICAL_LEDGER, 'https://fallback.invalid/canonical')
    value = f'[{selected}]' if shape == 'singleton_sequence' else f'" {selected} "'
    sidecar = f'semantic_review: {{path: {value}, sha256: {"0" * 64}}}\n'
    mapping_path = tmp_path / 'metadata/eml-mapping.yml'
    mapping_path.write_text(sidecar)
    mapping = eml._read_mapping_yaml(mapping_path)
    # The scalar helper coerces singleton sequences and trims strings, but the
    # governing schema requires a literal supported path before EML uses it.
    errors = []
    eml._schema_hash_sidecar(errors, 'semantic_review', mapping['semantic_review'],
                             eml.SUPPORTED_REVIEW_PATHS)
    assert any('semantic_review.path' in error for error in errors)
    before = raw_inputs(tmp_path)
    seen = []
    rows = ms.verify_sdp_semantic_iris(tmp_path, requester=lambda iri:
                                     (seen.append(iri) or {'status': 200, 'final_url': iri}))
    assert seen == ['https://fallback.invalid/canonical', 'https://fallback.invalid/root']
    assert rows.iri.tolist() == seen
    assert_inputs(before)


@requires_yaml
@pytest.mark.parametrize('tagged', ['semantic_review: !opaque {path: reviewed_semantic_selections.csv}\n',
                                   '!opaque [value]\n---\n42\n',
                                   'semantic_review: !unknown!value {path: reviewed_semantic_selections.csv}\n'])
def test_q62_unsupported_mapping_tag_refuses_before_transport_or_report(tmp_path, tagged):
    core(tmp_path)
    ledger(tmp_path, ROOT_LEDGER, 'https://selected.invalid/root')
    (tmp_path / 'metadata/eml-mapping.yml').write_text(tagged)
    report = tmp_path / REPORT
    report.parent.mkdir(parents=True)
    report.write_bytes(b'previous report\n')
    before = raw_inputs(tmp_path)
    seen = []
    with pytest.raises(ValueError, match='tag'):
        ms.verify_sdp_semantic_iris(tmp_path, requester=lambda iri:
                                  (seen.append(iri) or {'status': 200, 'final_url': iri}))
    assert seen == [] and report.read_bytes() == b'previous report\n'
    assert_inputs(before)


@requires_yaml
@pytest.mark.parametrize('obstruction', ['missing', 'outside'])
def test_selected_ledger_must_exist_inside_package_before_transport(tmp_path, obstruction):
    root = tmp_path / 'sdp'
    core(root)
    ledger(root, ROOT_LEDGER, 'https://unused.invalid/legacy')
    (root / 'metadata/eml-mapping.yml').write_text(f'semantic_review: {{path: {CANONICAL_LEDGER}}}\n')
    (root / 'reproducibility').mkdir()
    if obstruction == 'outside':
        outside = tmp_path / 'external.csv'
        outside.write_text('decision,iri\naccepted,https://outside.invalid/resource\n')
        (root / CANONICAL_LEDGER).symlink_to(outside)
    report = root / REPORT
    report.parent.mkdir(parents=True)
    report.write_bytes(b'previous report\n')
    before = raw_inputs(root)
    seen = []
    with pytest.raises((FileNotFoundError, ValueError)):
        ms.verify_sdp_semantic_iris(root, requester=lambda iri:
                                  (seen.append(iri) or {'status': 200, 'final_url': iri}))
    assert seen == [] and report.read_bytes() == b'previous report\n'
    assert_inputs(before)


def test_mapping_dependency_is_optional_and_actionable_only_on_use(tmp_path, monkeypatch):
    core(tmp_path)
    iri = 'https://core.invalid/root'
    ledger(tmp_path, ROOT_LEDGER, iri)
    # Genuine HTTPX/PyYAML-absent core runs this path as well; in extras, a local
    # import guard isolates the same missing-PyYAML boundary without an install.
    actual_import = builtins.__import__
    def absent_yaml(name, *args, **kwargs):
        if name == 'yaml' or name.startswith('yaml.'):
            raise ModuleNotFoundError("No module named 'yaml'", name='yaml')
        return actual_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', absent_yaml)
    assert ms.verify_sdp_semantic_iris(tmp_path, requester=lambda value:
                                     {'status': 200, 'final_url': value}).iri.tolist() == [iri]
    report = tmp_path / REPORT
    old_report = report.read_bytes()
    (tmp_path / 'metadata/eml-mapping.yml').write_text(f'semantic_review: {{path: {ROOT_LEDGER}}}\n')
    seen = []
    with pytest.raises(ImportError, match=r'metasalmonpy\[eml\]'):
        ms.verify_sdp_semantic_iris(tmp_path, requester=lambda value:
                                  (seen.append(value) or {'status': 200, 'final_url': value}))
    assert seen == [] and report.read_bytes() == old_report
