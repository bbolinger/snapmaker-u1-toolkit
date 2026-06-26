from u1_material_picker import status_to_options, rgba_to_color_name

def _status(exists):
    return {'print_task_config':{'filament_exist':exists,'filament_type':['PETG','PETG','PLA','PETG'],'filament_vendor':['A','B','C','D'],'filament_color_rgba':['FFFFFFFF','000000FF','1B5CB0FF','E2DEDBFF']}}

def test_only_loaded_slots_appear():
    opts=status_to_options(_status([False, True, True, False]), 'PETG')
    assert [o['value'] for o in opts]==['T1','T2']

def test_recommend_first_matching_material():
    opts=status_to_options(_status([True, True, True, False]), 'PLA')
    rec=[o['value'] for o in opts if o.get('recommended')]
    assert rec==['T2']

def test_empty_when_nothing_loaded():
    assert status_to_options(_status([False,False,False,False]))==[]

def test_rgba_hex_translated_to_human_color_name():
    opts=status_to_options(_status([True, True, True, True]))
    by_tool={o['value']: o for o in opts}
    assert by_tool['T0']['color_name']=='white'
    assert by_tool['T1']['color_name']=='black'
    assert by_tool['T2']['color_name']=='blue'
    assert by_tool['T3']['color_name']=='beige'
    for o in opts:
        assert o['color_name'] not in o['label'] or o['color_rgba'] not in o['label']
        assert 'FFFFFFFF' not in o['label'] and '000000FF' not in o['label']

def test_color_rgba_preserved_on_option_dict():
    opts=status_to_options(_status([True, False, False, False]))
    assert opts[0]['color_rgba']=='FFFFFFFF'
    assert opts[0]['color_name']=='white'

def test_rgba_to_color_name_handles_edge_cases():
    assert rgba_to_color_name('F78E0EFF')=='orange'
    assert rgba_to_color_name('#FFFFFF')=='white'
    assert rgba_to_color_name('')=='unknown'
    assert rgba_to_color_name(None)=='unknown'
    assert rgba_to_color_name('not-a-hex')=='not-a-hex'
