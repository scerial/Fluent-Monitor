import sys

from fluentmon import cli

if __name__ == "__main__":
    cli.main(["serve", *sys.argv[1:]])
