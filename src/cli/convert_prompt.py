import json
from tqdm import tqdm
from transformers import AutoTokenizer

def convert(args):
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_name)
    with open(args.input_data, "r") as f:
        data = [json.loads(x) for x in f.readlines()]
    ar_data = []
    for datum in tqdm(data):
        chat = datum["conversations"]
        for c in chat:
            c['content'] = c['message']
            del c['message']

        text = tokenizer.apply_chat_template(chat, tokenize=False)
        ar_data.append(text)
    with open(args.output_data, "w") as f:
        for datum in ar_data:
            f.write(json.dumps({"text": datum}) + "\n")
            
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-data", type=str, required=True, help="Path to the YAML or python config file")
    parser.add_argument("--output-data", type=str, required=True, help="Path to the YAML or python config file")
    parser.add_argument("--tokenizer-name", type=str, required=True)
    args = parser.parse_args()
    convert(args)