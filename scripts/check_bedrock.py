"""Check the Bedrock path GiveRight actually uses, one layer at a time.

Run this after each setup step. It stops at the first thing that is missing and
tells you what to do about it, so you are never guessing which layer is broken:

    credentials -> region -> model access -> invoke -> structured output

    python scripts/check_bedrock.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")
from giveright.llm import AGENT_MODEL_ID, VISION_MODEL_ID  # noqa: E402

MODEL_ID = AGENT_MODEL_ID
UNDERLYING = MODEL_ID.split(".", 1)[1] if MODEL_ID[:3] in ("us.", "eu.") else MODEL_ID

OK, BAD = "\033[32mOK\033[0m", "\033[31mFAILED\033[0m"


def step(n: int, what: str) -> None:
    print(f"\n[{n}] {what}")


def fail(why: str, fix: str) -> None:
    print(f"    {BAD}  {why}")
    print(f"    fix: {fix}")
    sys.exit(1)


def main() -> int:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    step(1, "Credentials")
    session = boto3.Session()
    if session.get_credentials() is None:
        fail(
            "boto3 cannot find any credentials.",
            "write ~/.aws/credentials with an access key, or export "
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY",
        )
    print(f"    {OK}  found via {session.get_credentials().method}")

    step(2, "Region")
    region = session.region_name
    if not region:
        fail(
            "no region configured.",
            "put `region = us-east-1` in ~/.aws/config, or export AWS_REGION=us-east-1",
        )
    if not region.startswith("us-"):
        print(f"    note: region is {region}, but {MODEL_ID} is a US inference")
        print("          profile. Use a us-* region.")
    print(f"    {OK}  {region}")

    step(3, "Identity")
    try:
        who = session.client("sts").get_caller_identity()
        print(f"    {OK}  {who['Arn']}")
    except (ClientError, NoCredentialsError) as exc:
        fail(str(exc), "the access key is wrong, disabled, or deleted")

    step(4, "Model access")
    try:
        bedrock = session.client("bedrock")
        ids = {m["modelId"] for m in bedrock.list_foundation_models()["modelSummaries"]}
        if UNDERLYING in ids:
            print(f"    {OK}  {UNDERLYING} is listed in {region}")
        else:
            print(f"    note: {UNDERLYING} is not listed in {region}.")
            print("          It may still work through the inference profile; step 5 decides.")
    except ClientError as exc:
        print(f"    note: could not list models ({exc.response['Error']['Code']}).")
        print("          Not fatal -- step 5 is the real test.")

    step(5, f"Invoke the agent model ({MODEL_ID})")
    try:
        from strands import Agent
        from strands.models import BedrockModel

        agent = Agent(
            model=BedrockModel(model_id=MODEL_ID),
            system_prompt="Answer with a single word.",
        )
        reply = agent("Say the word: ready")
        print(f"    {OK}  the model replied: {str(reply).strip()[:60]}")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"]["Message"]

        if "being verified" in message or "account is currently being" in message:
            fail(
                f"{code}: {message}",
                "nothing is wrong with your setup -- this is AWS's new-account "
                "hold, which gates Bedrock at the account level regardless of "
                "IAM policy or model access. It usually clears within 2 hours. "
                "Re-run this script later; if it is still blocked after that, "
                "email aws-verification@amazon.com.",
            )

        if "use case details" in message:
            fail(
                f"{code}: {message}",
                "This model family needs a one-time use case form for "
                "the account. Bedrock console -> Model access -> that model's "
                "entry -> submit the use case details form. It is "
                "approved automatically in most cases; allow ~15 minutes to "
                "propagate, then re-run this script.",
            )

        if "not allowed for this account" in message:
            fail(
                f"{code}: {message}",
                "Bedrock is disabled for this AWS account, which is an "
                "account-level state -- no IAM policy or region change fixes "
                "it. Check ~/.aws/credentials is pointing at the account you "
                "expect (the identity in step 3 above), and that Bedrock is "
                "enabled there.",
            )

        if code in ("AccessDeniedException", "AccessDenied"):
            fail(
                f"{code}: {message}",
                "either model access is not enabled (Bedrock console -> Model "
                "access -> enable Claude Sonnet 4.5) or your IAM policy is "
                "missing bedrock:InvokeModel / InvokeModelWithResponseStream. "
                "Cross-region profiles need permission on the profile AND the "
                "foundation model in every region it spans -- Resource: \"*\" "
                "is the quickest way to rule this out.",
            )
        if code == "ValidationException":
            fail(
                f"{code}: {message}",
                f"{MODEL_ID} may not exist in {region}. Try us-east-1 or us-west-2.",
            )
        fail(f"{code}: {exc}", "see the message above")
    except Exception as exc:  # noqa: BLE001 -- this is a diagnostic
        fail(f"{type(exc).__name__}: {exc}", "unexpected; paste this output")

    step(6, f"Structured output ({VISION_MODEL_ID}) -- what identify_pile uses")
    try:
        from giveright.vision import Pile

        agent = Agent(
            model=BedrockModel(model_id=VISION_MODEL_ID),
            system_prompt="You identify donatable items in photographs.",
            structured_output_model=Pile,
        )
        result = agent([{"text": "Reply with an empty item list."}])
        if result.structured_output is None:
            fail("the model returned no structured output.",
                 "unexpected; paste this output")
        print(f"    {OK}  structured output works "
              f"({len(result.structured_output.items)} items parsed)")
    except Exception as exc:  # noqa: BLE001
        fail(f"{type(exc).__name__}: {exc}", "paste this output")

    print("\n\033[32mEverything GiveRight needs is working.\033[0m")
    print("Next:  .venv/bin/python -m giveright.demo --agent --photo <your-photo>.jpg")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
