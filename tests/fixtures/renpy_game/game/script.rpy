define e = Character("Eileen")
define m = Character("Mysterious Voice")

label start:
    "The rain had not stopped for three days."
    e "Hello, [player_name]. Welcome to the {b}manor{/b}."
    e "She said: \"Do not go there.\""
    menu:
        "Go inside":
            jump inside
        "Wait outside":
            jump outside

    python:
        secret_code = "THIS_IS_CODE_NOT_TEXT"
        x = 1 + 2
    $ y = "ALSO_CODE"
    return
