# raic-cli
Russian AI Cup command line tool

## Usage
Install packages from requirements file, run:

```
pip3 install -r requirements.txt
```

Edit config file `config.yaml`

### Create games

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

### Find games

To find games, run:
```
./raic_cli.py find-games $USER
```
![image](https://user-images.githubusercontent.com/1968460/103151320-2369da80-478e-11eb-9b2c-761e4b34793c.png)



or set some params for filter:
```
./raic_cli.py find-games $USER --nolimit --datetime-from '26 Dec' --rank 2 --contest finals
```

First query for user will be long. Use `limit` or `datetime-from` for more fast response (without iterating over all games).
