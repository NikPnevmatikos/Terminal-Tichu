"""``python -m tichu`` — points at the two real entry points."""

print(
    """Terminal Tichu

  host a table:   python -m tichu.server [--port 4271] [--target 1000] [--bots N]
  join a table:   python -m tichu.client --host <server> [--port 4271] --name <you>
  watch a table:  python -m tichu.client --host <server> --spectate

See README.md for the rules reference and the full command language."""
)
