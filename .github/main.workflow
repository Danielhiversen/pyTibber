workflow "on push" {
  on = "push"
  resolves = ["GitHub Action for pylint"]
}

action "GitHub Action for pylint" {
  uses = "cclauss/GitHub-Action-for-pylint@master"
  args = "pylint"
}
