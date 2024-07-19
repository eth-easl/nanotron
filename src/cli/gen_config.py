if __name__=="__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt-path", type=str, required=True, help="Path to the YAML or python config file")