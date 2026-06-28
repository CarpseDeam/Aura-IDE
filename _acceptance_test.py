from aura.skills.utility import _compute_utility_from_rows, SourceUtility

# Row format: {status: str, task_kind: str|None, included_source_ids: str(JSON list)}

# 3 sources: s1, s2, s3
# 4 rows in 'refactor' terrain, 4 in 'debug' terrain
# s3 must appear in at least one row's included_source_ids to be collected.
# We add it to the debug completed row so it's collected but not loaded in refactor.
rows = [
    # refactor terrain: s1 loaded, s2 not loaded, s3 not loaded
    {'status': 'completed', 'task_kind': 'refactor', 'included_source_ids': '["s1"]'},
    {'status': 'completed_with_caveats', 'task_kind': 'refactor', 'included_source_ids': '["s1"]'},
    {'status': 'harness_error', 'task_kind': 'refactor', 'included_source_ids': '["s1"]'},
    {'status': 'completed', 'task_kind': 'refactor', 'included_source_ids': '[]'},
    # debug terrain: s2 loaded, s1 not loaded, s3 loaded (to collect s3)
    {'status': 'completed', 'task_kind': 'debug', 'included_source_ids': '["s2", "s3"]'},
    {'status': 'validation_failed', 'task_kind': 'debug', 'included_source_ids': '["s2"]'},
    {'status': 'completed', 'task_kind': 'debug', 'included_source_ids': '[]'},
]

result = _compute_utility_from_rows(rows, min_arm=2)

# s1: in refactor, loaded=3 (2 success, 1 fail), not_loaded=1 (1 success)
#   loaded_rate = 2/3 = 0.667, not_loaded_rate = 1/1 = 1.0
#   lift = 0.667 - 1.0 = -0.333
u1 = result.get('s1')
assert u1 is not None, 's1 should be measured'
assert u1.loaded_n == 3, f's1 loaded_n expected 3 got {u1.loaded_n}'
assert u1.not_loaded_n == 1, f's1 not_loaded_n expected 1 got {u1.not_loaded_n}'
assert abs(u1.lift - (-1/3)) < 0.001, f's1 lift expected -0.333 got {u1.lift}'
assert u1.status == 'measured', f's1 status expected measured got {u1.status}'

# s2: in debug, loaded=2 (1 success, 1 fail), not_loaded=1 (1 success)
#   loaded_rate = 0.5, not_loaded_rate = 1.0
#   lift = 0.5 - 1.0 = -0.5
u2 = result.get('s2')
assert u2 is not None, 's2 should be measured'
assert u2.loaded_n == 2, f's2 loaded_n expected 2 got {u2.loaded_n}'
assert u2.not_loaded_n == 1, f's2 not_loaded_n expected 1 got {u2.not_loaded_n}'
assert abs(u2.lift - (-0.5)) < 0.001, f's2 lift expected -0.5 got {u2.lift}'
assert u2.status == 'measured', f's2 status expected measured got {u2.status}'

# s3: in refactor terrain (largest combined sample), loaded=0, not_loaded=4 (3 success, 1 fail)
# not_loaded_n=4 >= min_arm=2 but loaded_n=0 < min_arm=2 -> insufficient
u3 = result.get('s3')
assert u3 is not None, 's3 should be present'
assert u3.status == 'insufficient', f's3 status expected insufficient got {u3.status}'
assert u3.loaded_n == 0, f's3 loaded_n expected 0 got {u3.loaded_n}'
assert u3.not_loaded_n == 4, f's3 not_loaded_n expected 4 got {u3.not_loaded_n}'
assert u3.lift is None, 's3 lift should be None'

print('All assertions passed')
