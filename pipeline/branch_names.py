"""Single definition of the story branch naming convention."""


def story_branch(story_key: str) -> str:
    return f"agent/{story_key.lower()}"
