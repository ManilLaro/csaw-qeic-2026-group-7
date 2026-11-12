from conftest import ROOT
def test_public_release_has_no_private_paths():
    for p in ROOT.rglob('*'):
        if set(p.relative_to(ROOT).parts)&{'__pycache__','.pytest_cache','.venv','outputs'}: continue
        assert not set(p.relative_to(ROOT).parts)&{'organizer'}
