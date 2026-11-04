"""Start the hosted Apify Actor (used by `apify run` and the Docker image)."""
import asyncio

from deepsift.actor import main

if __name__ == "__main__":
    asyncio.run(main())
