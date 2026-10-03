"""Conservative title hints; account eligibility is always confirmed by a person."""
import re

NEW_USER_WORDS = re.compile(r"新人|新客|新用户|首单|首购|首次下单|新注册|新人券|新客券")


def detected_offer_type(title):
    return "suspected_new_user" if NEW_USER_WORDS.search(title) else "unknown"
