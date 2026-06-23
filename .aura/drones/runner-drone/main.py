import json, sys
def run(payload):
    return {'ok': True, 'goal': payload.get('goal'), 'message': 'ran'}
if __name__ == '__main__':
    payload = json.loads(sys.stdin.read())
    result = run(payload)
    print(json.dumps(result))
