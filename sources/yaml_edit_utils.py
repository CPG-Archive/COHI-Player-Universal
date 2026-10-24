####################################################
# COHI-Player-Universal
# V1.0.0, 24.10.2026
# Author: Claus Peter Gallenmiller, cpg-radio@gmx.de
####################################################

import re


def replace_yaml_value(text, key, new_value):
    pattern = re.compile(rf'^({re.escape(key)}:\s*).*$', re.MULTILINE)
    if not pattern.search(text):
        return None
    return pattern.sub(lambda m: m.group(1) + str(new_value), text, count=1)
