"""Basic langchain-wasmsh example."""

from langchain_wasmsh import WasmshSandbox


def main() -> None:
    backend = WasmshSandbox(
        initial_files={"/workspace/data.txt": b"hello from wasmsh\n"}
    )
    try:
        result = backend.execute(
            "cat data.txt && python3 -c \"print(open('/workspace/data.txt').read().strip())\""
        )
        print(result.output)
    finally:
        backend.close()


if __name__ == "__main__":
    main()
