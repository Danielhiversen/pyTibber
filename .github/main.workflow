
workflow "flake8" {
  on = "push"
  resolves = ["GitHub Action for Flake8"]
}

action "GitHub Action for Flake8" {
  uses = "cclauss/GitHub-Action-for-Flake8@master"
  args = "flake8 tibber"
}
