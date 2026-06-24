from pathlib import Path
from u1_profile_picker import list_profiles

def test_strength_hint_recommends_020(tmp_path):
    (tmp_path/'community_020_strength_u1_textured_pei.json').write_text('{}')
    (tmp_path/'community_016_optimal_u1_textured_pei.json').write_text('{}')
    opts=list_profiles(tmp_path, class_hint='bracket holder')
    assert [o for o in opts if o.get('recommended')][0]['value'].startswith('020_strength')

def test_cosmetic_hint_recommends_016(tmp_path):
    (tmp_path/'community_020_strength_u1_textured_pei.json').write_text('{}')
    (tmp_path/'community_016_optimal_u1_textured_pei.json').write_text('{}')
    opts=list_profiles(tmp_path, class_hint='cosmetic')
    assert [o for o in opts if o.get('recommended')][0]['value'].startswith('016_optimal')
