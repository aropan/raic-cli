# raic-cli
Russian AI Cup command line tool

## Usage
Install packages from requirements file, run:

```
pip3 install -r requirements.txt
```

Edit config file `config.yaml`, as example:
```yaml
# create-game
users:
  - username: aropan
    # strategy: 6

  - query: top
    sources:
      - contest: round2
        number: 50
      - contest: sandbox
        number: 10
        without: finals

  # - query: random
  #   users:
  #     - username: Commandos
  #     - username: Recar
  #     - username: GreenTea
  #     - username: Romka

  # - query: suggest

formats:
  # - 4x1$${"preset":"Round1"}
  # - 4x1$${"preset":"Round2"}
  - 2x1$${"preset":"Finals"}
```

### Create game

To create a game, run:
```
./raic_cli.py create-game
```

or you can set number games:
```
./raic_cli.py create-game --limit 2
```

or without limit:
```
./raic_cli.py create-game --nolimit
```

See more options:
```
./raic_cli.py create-game -- --help
```

If something went wrong than error should be print.

