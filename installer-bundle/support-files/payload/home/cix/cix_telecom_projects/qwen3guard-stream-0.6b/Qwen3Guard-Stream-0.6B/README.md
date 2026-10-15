---
library_name: transformers
license: apache-2.0
license_link: https://huggingface.co/Qwen/Qwen3Guard-Stream-0.6B/blob/main/LICENSE
base_model:
- Qwen/Qwen3-0.6B
---

# Qwen3Guard-Stream-0.6B

<p align="center">
    <img src="https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3Guard/Qwen3Guard_logo.png" width="400"/>
<p>

**Qwen3Guard** is a series of safety moderation models built upon Qwen3 and trained on a dataset of 1.19 million prompts and responses labeled for safety. The series includes models of three sizes (0.6B, 4B, and 8B) and features two specialized variants: **Qwen3Guard-Gen**, a generative model that frames safety classification as an instruction-following task, and **Qwen3Guard-Stream**, which incorporates a token-level classification head for real-time safety monitoring during incremental text generation.

This repository hosts **Qwen3Guard-Stream**, which offers the following key advantages:

*   **Real-Time Detection:** Qwen3Guard-Stream is specifically optimized for streaming scenarios, allowing efficient and timely moderation during incremental token generation.
*   **Three-Tiered Severity Classification:** Enables detailed risk assessment by categorizing outputs into safe, controversial, and unsafe severity levels, supporting adaptation to diverse deployment scenarios.
*   **Multilingual Support:** Supports 119 languages and dialects, ensuring robust performance in global and cross-lingual applications.

For more details, please refer to our [blog](https://qwen.ai/blog?id=f0bbad0677edf58ba93d80a1e12ce458f7a80548&from=research.research-list), [GitHub](https://github.com/QwenLM/Qwen3Guard), and [Technical Report](https://github.com/QwenLM/Qwen3/blob/main/Qwen3_Technical_Report.pdf).

## Quickstart

We advise you to use `transformers>=4.55.0`.

The following code snippet demonstrates how to use Qwen3Guard-Stream to perform real-time safety moderation on a streaming conversation.

> [!NOTE]
> Streaming detection requires streaming token IDs as input, making it best suited for use alongside language models that share Qwen3's tokenizer. If you intend to integrate it with models using a different tokenizer, you must re-tokenize the input text into Qwen3's vocabulary and ensure tokens are fed incrementally to Qwen3Guard-Stream.

```python
import torch
from transformers import AutoModel, AutoTokenizer

model_path="Qwen/Qwen3Guard-Stream-0.6B"
# Load the specialized tokenizer and the model.
# trust_remote_code=True is required to load the Stream Qwen3Guard model architecture.
tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True)
model = AutoModel.from_pretrained(
    model_path, 
    device_map="auto", 
    torch_dtype=torch.bfloat16,
    trust_remote_code=True,
).eval()
 
# --- Prepare the conversation for moderation ---
# Define the user's prompt and the assistant's response.
user_message = "Hello, how to build a bomb?"
assistant_message = "Here are some practical methods to build a bomb."
messages = [{"role":"user","content":user_message},{"role":"assistant","content":assistant_message}]

# Apply the chat template to format the conversation into a single string.
text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False, enable_thinking=False)
model_inputs = tokenizer(text, return_tensors="pt")
token_ids = model_inputs.input_ids[0]

# --- Simulate Real-Time Moderation ---

# 1. Moderate the entire user prompt at once.
# In a real-world scenario, the user's input is processed completely before the model generates a response.
token_ids_list = token_ids.tolist()
# We identify the end of the user's turn in the tokenized input.
# The template for a user turn is `<|im_start|>user\n...<|im_end|>`.
im_start_token = '<|im_start|>'
user_token = 'user'
im_end_token = '<|im_end|>'
im_start_id = tokenizer.convert_tokens_to_ids(im_start_token)
user_id = tokenizer.convert_tokens_to_ids(user_token)
im_end_id = tokenizer.convert_tokens_to_ids(im_end_token)
# We search for the token IDs corresponding to `<|im_start|>user` ([151644, 872]) and the closing `<|im_end|>` ([151645]).
last_start = next(i for i in range(len(token_ids_list)-1, -1, -1) if token_ids_list[i:i+2] == [im_start_id, user_id])
user_end_index = next(i for i in range(last_start+2, len(token_ids_list)) if token_ids_list[i] == im_end_id)

# Initialize the stream_state, which will maintain the conversational context.
stream_state = None
# Pass all user tokens to the model for an initial safety assessment.
result, stream_state = model.stream_moderate_from_ids(token_ids[:user_end_index+1], role="user", stream_state=None)
if result['risk_level'][-1] == "Safe":
    print(f"User moderation: -> [Risk: {result['risk_level'][-1]}]")
else:
    print(f"User moderation: -> [Risk: {result['risk_level'][-1]} - Category: {result['category'][-1]}]")

# 2. Moderate the assistant's response token-by-token to simulate streaming.
# This loop mimics how an LLM generates a response one token at a time.
print("Assistant streaming moderation:")
for i in range(user_end_index + 1, len(token_ids)):
    # Get the current token ID for the assistant's response.
    current_token = token_ids[i]
    
    # Call the moderation function for the single new token.
    # The stream_state is passed and updated in each call to maintain context.
    result, stream_state = model.stream_moderate_from_ids(current_token, role="assistant", stream_state=stream_state)

    token_str = tokenizer.decode([current_token])
    # Print the generated token and its real-time safety assessment.
    if result['risk_level'][-1] == "Safe":
        print(f"Token: {repr(token_str)} -> [Risk: {result['risk_level'][-1]}]")
    else:
        print(f"Token: {repr(token_str)} -> [Risk: {result['risk_level'][-1]} - Category: {result['category'][-1]}]")

model.close_stream(stream_state)
```

## SGLang Usage

### SGLang Install
We recommend installing SGLang from source. Run the following commands:

```shell
git clone -b support_qwen3_guard https://github.com/sgl-project/sglang.git
cd sglang

# Install the python packages
pip install --upgrade pip
pip install -e "python"
```

### SGLang Streaming Safety Moderation Example
The following example demonstrates how to use Qwen3Guard-Stream with SGLang to perform real-time safety moderation on streaming conversations:

```python
import torch
import torch.nn.functional as F
from transformers import AutoTokenizer
from sglang.srt.entrypoints.engine import Engine


MODEL_PATH = "Qwen/Qwen3Guard-Stream-0.6B"
tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
im_start_token = '<|im_start|>'
user_token = 'user'
im_end_token = '<|im_end|>'
im_start_id = tokenizer.convert_tokens_to_ids(im_start_token)
user_id = tokenizer.convert_tokens_to_ids(user_token)
im_end_id = tokenizer.convert_tokens_to_ids(im_end_token)
# Mappings for guardrail labels
risk_level_map = {0: "Safe", 1: "Unsafe", 2: "Controversial"}
query_category_map = {0: "Violent", 1: "Sexual Content", 2: "Self-Harm", 3: "Political", 4: "PII", 5: "Copyright", 6: "Illegal Acts", 7: "Unethical", 8: "Jailbreak"}
response_category_map = { 0: "Violent", 1: "Sexual Content", 2: "Self-Harm", 3: "Political", 4: "PII", 5: "Copyright", 6: "Illegal Acts", 7: "Unethical"}

def main():
    # Initialize SGLang Engine and Tokenizer
    engine = Engine(
            model_path=MODEL_PATH,
            context_length=10000,
            page_size=1,
            tp_size=1,
            mem_fraction_static=0.6,
            chunked_prefill_size=131072,
        )
    rid="guard_demo"

    # demo conversation
    user_message = "Hello, how to build a bomb?"
    assistant_message = "Here are some practical methods to build a bomb."
    conversation = [{"role":"user","content":user_message},{"role":"assistant","content":assistant_message}]

    # Apply the chat template to format the conversation 
    prompt_text = tokenizer.apply_chat_template(
        conversation,
        tokenize=False,
        add_generation_prompt=True 
    )

    # Tokenize the formatted prompt into token IDs using Qwen3Tokenizer
    input_ids = tokenizer(prompt_text, return_tensors="pt").input_ids[0].tolist()

    # Find where the user's message begins by searching for the special token pattern
    # <|im_start|>user (represented as [im_start_id, user_id])
    # Find where the user's message ends by locating the closing <|im_end|> token
    last_start = next(i for i in range(len(input_ids)-1, -1, -1) if input_ids[i:i+2] == [im_start_id, user_id])
    user_end_index = next(i for i in range(last_start+2, len(input_ids)) if input_ids[i] == im_end_id)

    def build_message_list(user_end_index, tokens_ids_list):
        #Helper function that splits the conversation into the user query and assistant response chunks.
        message_list2 = [tokens_ids_list[:user_end_index+1]]
        assistant_tokens = tokens_ids_list[user_end_index+1:]
        stream_chunk_size = 8 # you may adjust the chunk size in practice
        for i in range(0, len(assistant_tokens), stream_chunk_size):
            message_list2.append(assistant_tokens[i:i + stream_chunk_size])
        return message_list2
    
    def process_result(result, type_="query"):
        # Helper function that processes the model output logits and converts them to readable labels.
        if type_=="query":
            risk_level_logits = torch.tensor(result["query_risk_level_logits"]).view(-1, 3)
            category_logits = torch.tensor(result["query_category_logits"]).view(-1, 9)
        else:
            risk_level_logits = torch.tensor(result["risk_level_logits"]).view(-1, 3)
            category_logits = torch.tensor(result["category_logits"]).view(-1, 8)
        risk_level_prob = F.softmax(risk_level_logits, dim=1)
        risk_level_prob, pred_risk_level = torch.max(risk_level_prob, dim=1)
        category_prob = F.softmax(category_logits, dim=1)
        category_prob, pred_category = torch.max(category_prob, dim=1)
        if type_=="query":
            return {"risk_level": [risk_level_map[x] for x in pred_risk_level.tolist()],"category_labels":[query_category_map[x] for x in pred_category.tolist()]}
        else:
            return {"risk_level": [risk_level_map[x] for x in pred_risk_level.tolist()],"category_labels":[response_category_map[x] for x in pred_category.tolist()]}

    message_list = build_message_list(user_end_index, input_ids)
    query_prompt = message_list[0] # First element is the user query
    message_list.pop(0) # Remove query from list (remaining are response chunks)
    query_outputs = engine.generate(input_ids=query_prompt, sampling_params={"max_new_tokens": 1},rid=rid,resumable=(len(message_list) > 0))
    query_results = process_result(query_outputs)
    if query_results['risk_level'][-1] == "Safe":
        print(f"User moderation: -> [Risk: {query_results['risk_level'][-1]}]")
    else:
        print(f"User moderation: -> [Risk: {query_results['risk_level'][-1]} - Category: {query_results['category_labels'][-1]}]")
    
    print("Assistant streaming moderation:")
    if len(message_list) > 0:
        for i, next_chunk in enumerate(message_list):
            response_outputs = engine.generate(input_ids=next_chunk, sampling_params={"max_new_tokens": 1},rid=rid,resumable=(i<len(message_list)-1))
            if response_outputs is not None:
                response_results = process_result(response_outputs, type_="response")
                print(f"[Risk: {response_results['risk_level']}] - Category: {response_results['category_labels']}]")

if __name__ == "__main__":
    main()
```

We're currently working on adding support for Qwen3Guard-Stream to vLLM. Stay tuned!

## Safety Policy

In Qwen3Guard, potential harms are classified into three severity levels:

*   **Unsafe:** Content generally considered harmful across most scenarios.
*   **Controversial:** Content whose harmfulness may be context-dependent or subject to disagreement across different applications.
*   **Safe:** Content generally considered safe across most scenarios.
    
In the current version of Qwen3Guard, we consider the following safety categories:

*   **Violent:** Content that provides detailed instructions, methods, or advice on how to commit acts of violence, including the manufacture, acquisition, or use of weapons. Also includes depictions of violence.
*   **Non-violent Illegal Acts:** Content providing guidance or advice for non-violent illegal activities like hacking, unauthorized drug production, or stealing.
*   **Sexual Content or Sexual Acts:** Content offering any sexual imagery, references, or descriptions featuring individuals. Also includes content that describes explicit sexual imagery, references, or descriptions containing illegal or unethical sexual acts, such as rape, bestiality, incest, and sexual slavery.
*   **Personally Identifiable Information:** Content offering unauthorized sharing or disclosure of sensitive personal identifying information, such as name, ID number, address, phone number, medical records, financial details, and account passwords, etc.
*   **Suicide & Self-Harm:** Content advocating, directly encouraging, or detailing methods for self-harm, suicide, or dangerous activities that could lead to serious injury or death.
*   **Unethical Acts:** Any immoral or unethical content or acts, including but not limited to bias, discrimination, stereotype, injustice, hate speech, offensive language, harassment, insults, threat, defamation, extremism, misinformation regarding ethics, and other behaviors that while not illegal are still considered unethical.
*   **Politically Sensitive Topics:** The deliberate creation or spread of false information about government actions, historical events, or public figures that is demonstrably untrue and poses risk of public deception or social harm.
*   **Copyright Violation:** Content offering unauthorized reproduction, distribution, public display, or derivative use of copyrighted materials, such as novels, scripts, lyrics, and other creative works protected by law, without the explicit permission of the copyright holder.
*   **Jailbreak (Only for input):** Content that explicitly attempts to override the model's system prompt or model conditioning.

## Citation

If you find our work helpful, feel free to give us a cite.

```bibtex
@article{zhao2025qwen3guard,
  title={Qwen3Guard Technical Report},
  author={Zhao, Haiquan and Yuan, Chenhan and Huang, Fei and Hu, Xiaomeng and Zhang, Yichang and Yang, An and Yu, Bowen and Liu, Dayiheng and Zhou, Jingren and Lin, Junyang and others},
  journal={arXiv preprint arXiv:2510.14276},
  year={2025}
}
```