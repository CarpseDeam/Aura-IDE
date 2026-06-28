from aura.skills.utility import _compute_utility_from_rows

rows = [
    {'status': 'completed', 'task_kind': 'refactor', 'included_source_ids': '["s1"]'},
    {'status': 'completed_with_caveats', 'task_kind': 'refactor', 'included_source_ids': '["s1"]'},
    {'status': 'harness_error', 'task_kind': 'refactor', 'included_source_ids': '["s1"]'},
    {'status': 'completed', 'task_kind': 'refactor', 'included_source_ids': '[]'},
    {'status': 'completed', 'task_kind': 'debug', 'included_source_ids': '["s2"]'},
    {'status': 'validation_failed', 'task_kind': 'debug', 'included_source_ids': '["s2"]'},
    {'status': 'completed', 'task_kind': 'debug', 'included_source_ids': '[]'},
]

result = _compute_utility_from_rows(rows, min_arm=2)
print(f'Keys in result: {sorted(result.keys())}')
for sid, u in sorted(result.items()):
    print(f'{sid}: loaded_n={u.loaded_n}, not_loaded_n={u.not_loaded_n}, lift={u.lift}, status={u.status}, task_kind={u.task_kind}')
