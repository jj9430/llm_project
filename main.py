from functools import partial
from importlib.metadata import version
import json
import os
import re
import time
import urllib
import tiktoken
import torch
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
import plotly.graph_objects as go 

from gpt_download import download_and_load_gpt2
from transformer_model import (
    calc_loss_loader,
    generate,
    GPTModel,
    load_weights_into_gpt,
    text_to_token_ids,
    train_model_simple,
    token_ids_to_text
)


class InstructionDataset(Dataset):
    def __init__(self, data, tokenizer):
        self.data = data

        self.encoded_texts = []
        for entry in data:
            instruction_plus_input = format_input(entry)
            response_text = f"\n\n### Response:\n{entry['output']}"
            full_text = instruction_plus_input + response_text
            self.encoded_texts.append(
                tokenizer.encode(full_text)
            )

    def __getitem__(self, index):
        return self.encoded_texts[index]

    def __len__(self):
        return len(self.data)


def custom_collate_fn(
    batch,
    pad_token_id=50256,
    ignore_index=-100,
    allowed_max_length=None,
    device="cpu"
):
    batch_max_length = max(len(item) + 1 for item in batch)

    inputs_lst, targets_lst = [], []

    for item in batch:
        new_item = item.copy()
        new_item += [pad_token_id]
        padded = new_item + [pad_token_id] * (batch_max_length - len(new_item))
        inputs = torch.tensor(padded[:-1])
        targets = torch.tensor(padded[1:])
        mask = targets == pad_token_id
        indices = torch.nonzero(mask).squeeze()
        if indices.numel() > 1:
            targets[indices[1:]] = ignore_index

        if allowed_max_length is not None:
            inputs = inputs[:allowed_max_length]
            targets = targets[:allowed_max_length]

        inputs_lst.append(inputs)
        targets_lst.append(targets)

    inputs_tensor = torch.stack(inputs_lst).to(device)
    targets_tensor = torch.stack(targets_lst).to(device)

    return inputs_tensor, targets_tensor


def download_and_load_file(file_path, url):
    if not os.path.exists(file_path):
        with urllib.request.urlopen(url) as response:
            text_data = response.read().decode("utf-8")
        with open(file_path, "w", encoding="utf-8") as file:
            file.write(text_data)

    with open(file_path, "r") as file:
        data = json.load(file)

    return data


def format_input(entry):
    instruction_text = (
        f"Below is an instruction that describes a task. "
        f"Write a response that appropriately completes the request."
        f"\n\n### Instruction:\n{entry['instruction']}"
    )

    input_text = f"\n\n### Input:\n{entry['input']}" if entry["input"] else ""

    return instruction_text + input_text


def plot_losses(epochs_seen, tokens_seen, train_losses, val_losses):
    interactive_fig = go.Figure()

    interactive_fig.add_trace(go.Scatter(
        x=epochs_seen, y=train_losses, mode='lines', name="Training Loss", line=dict(color='blue')
    ))
    interactive_fig.add_trace(go.Scatter(
        x=epochs_seen, y=val_losses, mode='lines', name="Validation Loss", line=dict(color='red', dash='dash')
    ))

    interactive_fig.update_layout(
        xaxis2=dict(
            overlaying="x",
            side="top",
            title="Tokens Seen",
            showgrid=False,
        ),
        title="Training and Validation Loss with Tokens Seen",
        xaxis=dict(title="Epochs"),
        yaxis=dict(title="Loss"),
        template="plotly_dark", 
    )

    interactive_fig.add_trace(go.Scatter(
        x=tokens_seen, y=train_losses, mode='lines', name="Tokens Seen (Training Loss)",
        line=dict(color='green', width=0),
        xaxis="x2"
    ))

    interactive_fig.show()

    interactive_plot_name = "interactive_loss_plot.html"
    interactive_fig.write_html(interactive_plot_name)
    print(f"Interactive plot saved as {interactive_plot_name}")


def main(test_mode=False):
    writer = SummaryWriter(log_dir="runs/finetuning_experiment")
    print()
    pkgs = [
        "matplotlib",
        "tiktoken",
        "torch", 
        "tqdm", 
        "tensorflow",
    ]
    for p in pkgs:
        print(f"{p} version: {version(p)}")
    print(50*"-")

    file_path = "/Users/joshuajoseph/email_templates.json"
    url = "https://raw.githubusercontent.com/jj9430/llm_project/refs/heads/main/email_dataset.json"
    data = download_and_load_file(file_path, url)
    print("Number of entries:", len(data))
    train_portion = int(len(data) * 0.85)
    test_portion = int(len(data) * 0.1)

    train_data = data[:train_portion]
    test_data = data[train_portion:train_portion + test_portion]
    val_data = data[train_portion + test_portion:]

    if test_mode:
        train_data = train_data[:10]
        val_data = val_data[:10]
        test_data = test_data[:10]

    print("Training set length:", len(train_data))
    print("Validation set length:", len(val_data))
    print("Test set length:", len(test_data))
    print(50*"-")

    tokenizer = tiktoken.get_encoding("gpt2")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")       
    print("Device:", device)
    print(50*"-")

    customized_collate_fn = partial(custom_collate_fn, device=device, allowed_max_length=1024)

    num_workers = 0
    batch_size = 8

    torch.manual_seed(123)

    train_dataset = InstructionDataset(train_data, tokenizer)
    train_loader = DataLoader(
        train_dataset,
        batch_size=batch_size,
        collate_fn=customized_collate_fn,
        shuffle=True,
        drop_last=True,
        num_workers=num_workers
    )

    val_dataset = InstructionDataset(val_data, tokenizer)
    val_loader = DataLoader(
        val_dataset,
        batch_size=batch_size,
        collate_fn=customized_collate_fn,
        shuffle=False,
        drop_last=False,
        num_workers=num_workers
    )

    if test_mode:
        BASE_CONFIG = {
            "vocab_size": 50257,
            "context_length": 120,
            "drop_rate": 0.0,
            "qkv_bias": False,
            "emb_dim": 12,
            "n_layers": 1,
            "n_heads": 2
        }
        model = GPTModel(BASE_CONFIG)
        model.eval()
        device = "cpu"
        CHOOSE_MODEL = "Small test model"

    else:
        BASE_CONFIG = {
            "vocab_size": 50257, 
            "context_length": 1024, 
            "drop_rate": 0.0,    
            "qkv_bias": True     
        }

        model_configs = {
            "gpt2-small (124M)": {"emb_dim": 768, "n_layers": 12, "n_heads": 12},
            "gpt2-medium (355M)": {"emb_dim": 1024, "n_layers": 24, "n_heads": 16},
            "gpt2-large (774M)": {"emb_dim": 1280, "n_layers": 36, "n_heads": 20},
            "gpt2-xl (1558M)": {"emb_dim": 1600, "n_layers": 48, "n_heads": 25},
        }

        CHOOSE_MODEL = "gpt2-medium (355M)"

        BASE_CONFIG.update(model_configs[CHOOSE_MODEL])

        model_size = CHOOSE_MODEL.split(" ")[-1].lstrip("(").rstrip(")")
        settings, params = download_and_load_gpt2(model_size=model_size, models_dir="gpt2")

        model = GPTModel(BASE_CONFIG)
        load_weights_into_gpt(model, params)
        model.eval()
        model.to(device)

    print("Loaded model:", CHOOSE_MODEL)
    print(50*"-")

    with torch.no_grad():
        train_loss = calc_loss_loader(train_loader, model, device, num_batches=5)
        val_loss = calc_loss_loader(val_loader, model, device, num_batches=5)

    print("   Training loss:", train_loss)
    print("   Validation loss:", val_loss)

    start_time = time.time()
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.00005, weight_decay=0.1)

    num_epochs = 2

    torch.manual_seed(123)
    train_losses, val_losses, tokens_seen = train_model_simple(
        model, train_loader, val_loader, optimizer, device,
        num_epochs=num_epochs, eval_freq=5, eval_iter=5,
        start_context=format_input(val_data[0]), tokenizer=tokenizer
    )

    end_time = time.time()
    execution_time_minutes = (end_time - start_time) / 60
    print(f"Training completed in {execution_time_minutes:.2f} minutes.")

    for epoch in range(num_epochs):
        writer.add_scalar('Loss/train', train_losses[epoch], epoch)
        writer.add_scalar('Loss/val', val_losses[epoch], epoch)

    for epoch in range(num_epochs):
        writer.add_scalar('Learning Rate', optimizer.param_groups[0]['lr'], epoch)

    epochs_tensor = torch.linspace(0, num_epochs, len(train_losses))
    plot_losses(epochs_tensor, tokens_seen, train_losses, val_losses)
    print(50*"-")

    print("Generating responses")
    for i, entry in tqdm(enumerate(test_data), total=len(test_data)):

        input_text = format_input(entry)

        token_ids = generate(
            model=model,
            idx=text_to_token_ids(input_text, tokenizer).to(device),
            max_new_tokens=256,
            context_size=BASE_CONFIG["context_length"],
            eos_id=50256
        )
        generated_text = token_ids_to_text(token_ids, tokenizer)
        response_text = generated_text[len(input_text):].replace("### Response:", "").strip()

        test_data[i]["model_response"] = response_text

    test_data_path = "instruction-data-with-response-standalone.json"
    with open(test_data_path, "w") as file:
        json.dump(test_data, file, indent=4)
    print(f"Responses saved as {test_data_path}")

    file_name = f"{re.sub(r'[ ()]', '', CHOOSE_MODEL) }-sft-standalone.pth"
    torch.save(model.state_dict(), file_name)
    print(f"Model saved as {file_name}")

    writer.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Finetune a GPT model for classification")
    parser.add_argument(
        "--test_mode", default=False, action="store_true",
        help=("This flag runs the model in test mode for internal testing purposes. "
              "Otherwise, it runs the model as it is used in the chapter (recommended).")
    )
    args = parser.parse_args()

    main(args.test_mode)
