"""Quick behavioral test for provider usability logic changes."""

import os
from aura.config import has_api_key, has_usable_provider_configuration, has_usable_provider_credentials

# 1. has_api_key for non-api-key providers should be False
result1 = has_api_key("claude_code")
assert result1 is False, f"FAIL: claude_code has_api_key = {result1} (expected False)"
print("PASS: has_api_key('claude_code') = False")

result2 = has_api_key("codex")
assert result2 is False, f"FAIL: codex has_api_key = {result2} (expected False)"
print("PASS: has_api_key('codex') = False")

# 2. has_api_key for api_key provider without key
result3 = has_api_key("deepseek")
assert result3 is False, f"FAIL: deepseek has_api_key = {result3} (expected False)"
print("PASS: has_api_key('deepseek') = False (no key)")

# 3. No usable provider on clean env
result4 = has_usable_provider_configuration()
assert result4 is False, f"FAIL: usable config = {result4} (expected False)"
print("PASS: has_usable_provider_configuration() = False (clean)")

# 4. Individual provider checks
result5 = has_usable_provider_configuration("claude_code")
assert result5 is False, f"FAIL: claude_code usable = {result5}"
print("PASS: has_usable_provider_configuration('claude_code') = False")

result6 = has_usable_provider_configuration("codex")
assert result6 is False, f"FAIL: codex usable = {result6}"
print("PASS: has_usable_provider_configuration('codex') = False")

result7 = has_usable_provider_configuration("deepseek")
assert result7 is False, f"FAIL: deepseek usable = {result7}"
print("PASS: has_usable_provider_configuration('deepseek') = False (no key)")

# 5. Backward compat
assert has_usable_provider_credentials() == has_usable_provider_configuration()
print("PASS: has_usable_provider_credentials() == has_usable_provider_configuration()")

# 6. With env key set, deepseek should be usable
os.environ["DEEPSEEK_API_KEY"] = "sk-test-123"
assert has_api_key("deepseek") is True, "FAIL: deepseek has_api_key with env"
print("PASS: has_api_key('deepseek') = True (with env key)")

assert has_usable_provider_configuration() is True, "FAIL: usable with key"
print("PASS: has_usable_provider_configuration() = True (with deepseek key)")

assert has_usable_provider_configuration("deepseek") is True, "FAIL: deepseek usable with key"
print("PASS: has_usable_provider_configuration('deepseek') = True (with key)")

del os.environ["DEEPSEEK_API_KEY"]

# 7. has_api_key still works for default provider (deepseek) without key = False
assert has_api_key() is False
print("PASS: has_api_key() = False (no key, default deepseek)")

print("\n=== ALL BEHAVIORAL CHECKS PASSED ===")
