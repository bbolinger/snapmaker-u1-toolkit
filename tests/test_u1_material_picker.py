from u1_material_picker import status_to_options

def _status(exists):
    return {'print_task_config':{'filament_exist':exists,'filament_type':['PETG','PETG','PLA','PETG'],'filament_vendor':['A','B','C','D'],'filament_color_rgba':['red','black','blue','white']}}

def test_only_loaded_slots_appear():
    opts=status_to_options(_status([False, True, True, False]), 'PETG')
    assert [o['value'] for o in opts]==['T1','T2']

def test_recommend_first_matching_material():
    opts=status_to_options(_status([True, True, True, False]), 'PLA')
    rec=[o['value'] for o in opts if o.get('recommended')]
    assert rec==['T2']

def test_empty_when_nothing_loaded():
    assert status_to_options(_status([False,False,False,False]))==[]
