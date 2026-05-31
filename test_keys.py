"""
Keyboard serialization diagnostic — run on the SERVER (Windows).

    python test_keys.py

Press keys and watch what MegaMind would forward. In particular press:
    Shift + 2        (expect '@' on a US layout)
    Shift + a        (expect 'A')
    just  2
    just  a

For each event it prints the raw pynput key and the serialized payload we send
to the client. The key question for the Shift+2 bug:

  - Does pressing '2' WHILE shift is held report  char='@'  (resolved)  ...
  - ... or  char='2'  (base, unresolved)?

ESC to quit.
"""

from pynput import keyboard


def serialize_key(key) -> dict:
    if hasattr(key, "char") and key.char is not None:
        return {"kind": "char", "char": key.char}
    elif hasattr(key, "vk") and key.vk is not None:
        return {"kind": "vk", "vk": key.vk}
    elif hasattr(key, "name") and key.name is not None:
        return {"kind": "special", "name": key.name}
    return {"kind": "unknown"}


def on_press(key):
    print(f"PRESS    {key!r:30} -> {serialize_key(key)}")


def on_release(key):
    print(f"RELEASE  {key!r:30} -> {serialize_key(key)}")
    if key == keyboard.Key.esc:
        return False


print(__doc__)
print("Listening... (press Shift+2, Shift+a, 2, a; ESC to quit)\n")
with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()
