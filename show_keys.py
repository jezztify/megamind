"""
show_keys.py — Display keyboard strokes in real time.

Usage:
    python show_keys.py

Press Ctrl+C (or Esc) to exit.
"""
import sys
from pynput.keyboard import Key, Listener

def on_press(key):
    try:
        # Printable character
        print(f"[{key.char}]")
    except AttributeError:
        # Special key (space, enter, shift, etc.)
        print(f"<{key.name}>")

    if key == Key.esc:
        return False  # stop listener

def on_release(key):
    if key == Key.esc:
        return False

print("Capturing keystrokes... Press Esc to exit.")
with Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()
print("Done.")
