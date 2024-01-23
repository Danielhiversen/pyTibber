workflow "flake8" {
  on = "push"
  resolves = ["The Python Action"]
}

action "GitHub Action for Flake8" {
  uses = "cclauss/GitHub-Action-for-Flake8@master"
  args = "flake8 tibber"
}

action "The Python Action" {
  uses = "Gr1N/the-python-action@0.4.0"
  needs = ["GitHub Action for Flake8"]
  args = "tox -e lint"
  env = {
    PYTHON_VERSION = "3.11"
  }
}
