# SPDX-License-Identifier: GPL-3.0-or-later
"""Literal shapes for find_string_literals; this docstring says EFLASH ERASE."""  # LIT: docstring

PLAIN = "EFLASH ERASE"  # LIT: string
AS_BYTES = b"EFLASH ERASE\n"  # LIT: bytes
IMPLICIT = "EFLASH " "ERASE"  # LIT: string
CONCAT = "EFLASH " + "ERASE"  # LIT: concat
LONGER = "SEND " + "EFLASH " + "ERASE" + ";"  # LIT: concat
BYTES_CONCAT = b"EFLASH " + b"ERASE"  # LIT: concat
NOT_THIS = "EFLASH_ERASE"
MIXED = "EFLASH " + b"ERASE".decode()
# EFLASH ERASE in a comment is not a literal.


def formatted(arg):
    return f"EFLASH ERASE {arg}"  # LIT: fstring


def placeholder(arg):
    # A placeholder stands for a value the scanner cannot know: it reads "{...}".
    return f"EFLASH {arg}"


class Documented:
    """Class docstring: EFLASH ERASE."""  # LIT: docstring

    def method(self):
        """Method docstring: EFLASH ERASE."""  # LIT: docstring
        return "EFLASH ERASE"  # LIT: string
