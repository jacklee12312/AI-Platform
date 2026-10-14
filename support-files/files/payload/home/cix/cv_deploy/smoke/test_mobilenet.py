import argparse
import json
import time

import numpy as np
from PIL import Image
from NOE_Engine import EngineInfer


def preprocess(image_path: str) -> np.ndarray:
    image = Image.open(image_path).convert("RGB").resize((224, 224))
    array = np.asarray(image, dtype=np.float32) / 255.0
    mean = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)
    array = (array - mean) / std
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()

    tensor = preprocess(args.image)
    model = EngineInfer(args.model)
    try:
        started = time.perf_counter()
        outputs = model.forward(tensor)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logits = np.asarray(outputs[0])
        result = {
            "ok": True,
            "input_shape": list(tensor.shape),
            "output_shapes": [list(np.asarray(output).shape) for output in outputs],
            "top1_index": int(np.argmax(logits)),
            "elapsed_ms": round(elapsed_ms, 3),
        }
        print(json.dumps(result, ensure_ascii=False))
    finally:
        model.clean()


if __name__ == "__main__":
    main()
