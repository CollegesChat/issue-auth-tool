from tomllib import load

with open('config.toml', 'rb') as f:
    config = load(f)
setting = config['settings']