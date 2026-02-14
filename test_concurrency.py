import asyncio
import httpx
import time

async def make_request(i):
    async with httpx.AsyncClient() as client:
        try:
            # Use keepalive endpoint as it's lightweight
            resp = await client.head("http://127.0.0.1:7861/keepalive", timeout=5.0)
            return resp.status_code
        except Exception as e:
            return str(e)

async def main():
    start = time.time()
    tasks = []
    for i in range(50):
        tasks.append(make_request(i))

    results = await asyncio.gather(*tasks)
    duration = time.time() - start

    success = [r for r in results if r == 200]
    failures = [r for r in results if r != 200]

    print(f"Total: {len(results)}")
    print(f"Success: {len(success)}")
    print(f"Failures: {len(failures)}")
    if failures:
        print(f"Sample failure: {failures[0]}")
    print(f"Duration: {duration:.2f}s")

if __name__ == "__main__":
    asyncio.run(main())
