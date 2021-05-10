import tqdm
import time
import os.path as p

import torch
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from datasets import load_from_disk, concatenate_datasets, Dataset
from torch.utils.data import TensorDataset, DataLoader, RandomSampler
from transformers import (
    AutoModel,
    AutoConfig,
    BertConfig,
    BertModel,
    AutoTokenizer,
    BertTokenizer,
    BertPreTrainedModel,
    AdamW,
    TrainingArguments,
    get_linear_schedule_with_warmup,
)

from retrieval.dense import DenseRetrieval
from tokenization_kobert import KoBertTokenizer


def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs


class BertEncoder(BertPreTrainedModel):
    def __init__(self, config):
        super(BertEncoder, self).__init__(config)

        self.bert = BertModel(config)
        self.bert_proj = nn.Linear(config.hidden_size, 512)
        self.init_weights()

    def forward(self, input_ids, attention_mask=None, token_type_ids=None):
        outputs = self.bert(input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids)
        pooled_output = outputs[1]  # embedding 가져오기
        pooled_output = self.bert_proj(pooled_output)
        return pooled_output


class AutoEncoder(nn.Module):
    def __init__(self, model_name, config):
        super().__init__()
        self.backbone = AutoModel.from_pretrained(model_name, config=config)
        self.backbone_proj = nn.Linear(config.hidden_size, 512)
        self.init_weights()

    def forward(self, input_ids, attention_mask=None, token_type_ids=None):
        outputs = self.backbone(input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids)
        pooled_output = outputs[1]  # embedding 가져오기
        pooled_output = self.backbone_proj(pooled_output)
        return pooled_output


class KoelectraEncoder(nn.Module):
    def __init__(self, model_name, config):
        self.backbone = AutoModel.from_pretrained(model_name, config=config)
        self.backbone_proj = nn.Linear(config.hidden_size, 512)
        self.init_weights()

    def forward(self, input_ids, attention_mask=None, token_type_ids=None):
        outputs = self.backbone(input_ids, attention_mask=attention_mask, token_type_ids=token_type_ids)
        pooled_output = outputs[0][:, 0]  # 0번째 값이 [CLS] Token
        pooled_output = self.backbone_proj(pooled_output)
        return pooled_output


class DprRetrieval(DenseRetrieval):
    def __init__(self, args):
        super().__init__(args)
        self.backbone = "bert-base-multilingual-cased"
        self.tokenizer = BertTokenizer.from_pretrained(self.backbone)

    def _load_model(self):
        config = BertConfig.from_pretrained(self.backbone)
        p_encoder = BertEncoder.from_pretrained(self.backbone, config=config).cuda()
        q_encoder = BertEncoder.from_pretrained(self.backbone, config=config).cuda()
        return p_encoder, q_encoder

    def _get_encoder(self):
        config = BertConfig.from_pretrained(self.backbone)
        q_encoder = BertEncoder(config=config).cuda()
        return q_encoder

    def train(self, training_args, dataset, p_model, q_model):
        """ Sampling된 데이터 셋으로 학습 """

        train_sampler = RandomSampler(dataset)
        train_dataloader = DataLoader(
            dataset, sampler=train_sampler, batch_size=training_args.per_device_train_batch_size
        )

        optimizer_grouped_parameters = [{"params": p_model.parameters()}, {"params": q_model.parameters()}]
        optimizer = AdamW(optimizer_grouped_parameters, lr=training_args.learning_rate, eps=training_args.adam_epsilon)
        t_total = len(train_dataloader) // training_args.gradient_accumulation_steps * training_args.num_train_epochs
        scheduler = get_linear_schedule_with_warmup(
            optimizer, num_warmup_steps=training_args.warmup_steps, num_training_steps=t_total
        )

        global_step = 0

        p_model.train()
        q_model.train()

        p_model.zero_grad()
        q_model.zero_grad()

        torch.cuda.empty_cache()

        for epoch in range(training_args.num_train_epochs):
            train_loss = 0.0
            start_time = time.time()

            for step, batch in enumerate(train_dataloader):

                if torch.cuda.is_available():
                    batch = tuple(t.cuda() for t in batch)

                p_inputs = {"input_ids": batch[0], "attention_mask": batch[1], "token_type_ids": batch[2]}
                q_inputs = {"input_ids": batch[3], "attention_mask": batch[4], "token_type_ids": batch[5]}

                p_outputs = p_model(**p_inputs)
                q_outputs = q_model(**q_inputs)

                sim_scores = torch.matmul(q_outputs, torch.transpose(p_outputs, 0, 1))
                targets = torch.arange(0, training_args.per_device_train_batch_size).long()

                if torch.cuda.is_available():
                    targets = targets.to("cuda")

                sim_scores = F.log_softmax(sim_scores, dim=1)
                loss = F.nll_loss(sim_scores, targets)

                print(f"epoch: {epoch:02} step: {step:02} loss: {loss}", end="\r")
                train_loss += loss.item()

                loss.backward()
                optimizer.step()
                scheduler.step()
                p_model.zero_grad()
                q_model.zero_grad()
                global_step += 1

                torch.cuda.empty_cache()

            end_time = time.time()
            epoch_mins, epoch_secs = epoch_time(start_time, end_time)

            print(f"Epoch: {epoch + 1:02} | Time: {epoch_mins}m {epoch_secs}s")
            print(f"\tTrain Loss: {train_loss / len(train_dataloader):.4f}")

        return p_model, q_model

    def _exec_embedding(self):
        p_encoder, q_encoder = self._load_model()

        datasets = load_from_disk(p.join(self.args.path.train_data_dir, self.args.retriever.dense_train_dataset))
        tokenizer_input = self.tokenizer(datasets["train"][1]["context"], padding="max_length", truncation=True, max_length=512)

        print("tokenizer:", self.tokenizer.convert_ids_to_tokens(tokenizer_input["input_ids"]))

        train_dataset = datasets["train"]

        # (1) Train, Valid 데이터 셋 합쳐서 학습

        train_dataset = concatenate_datasets(
           [datasets["train"].flatten_indices(), datasets["validation"].flatten_indices()]
        )

        # (2) Train, Valid, KorQuad 데이터 셋 합쳐서 학습

        kor_datasets = load_from_disk(p.join(self.args.path.train_data_dir, "kor_dataset"))

        features = kor_datasets["train"].features
        new_dataset = Dataset.from_pandas(kor_datasets['train'].to_pandas(), features=features)

        concatenate_list = [new_dataset.flatten_indices()]

        train_dataset = Dataset.from_pandas(train_dataset.to_pandas(), features=features)
        train_dataset = concatenate_datasets([train_dataset.flatten_indices()] + concatenate_list)
        
        # TODO: 코드 수정해야 함, PR 빨리 되어라
        q_seqs = self.tokenizer(train_dataset["question"], padding="longest", truncation=True, max_length=512, return_tensors="pt")
        p_seqs = self.tokenizer(train_dataset["context"], padding="max_length", truncation=True, max_length=512, return_tensors="pt")

        train_dataset = TensorDataset(
            p_seqs["input_ids"],
            p_seqs["attention_mask"],
            p_seqs["token_type_ids"],
            q_seqs["input_ids"],
            q_seqs["attention_mask"],
            q_seqs["token_type_ids"],
        )

        args = TrainingArguments(
            output_dir="dense_retrieval",
            evaluation_strategy="epoch",
            learning_rate=1e-4,
            per_device_train_batch_size=16,
            per_device_eval_batch_size=4,
            num_train_epochs=10,
            weight_decay=0.01,
        )

        p_encoder, q_encoder = self.train(args, train_dataset, p_encoder, q_encoder)

        p_embedding = []

        for passage in tqdm.tqdm(self.contexts):  # wiki
            passage = self.tokenizer(passage, padding="max_length", truncation=True, max_length=512, return_tensors="pt").to("cuda")
            p_emb = p_encoder(**passage).to("cpu").detach().numpy()
            p_embedding.append(p_emb)

        p_embedding = np.array(p_embedding).squeeze()  # numpy
        return p_embedding, q_encoder


class DprKobertRetrieval(DprRetrieval):
    def __init__(self, args):
        super().__init__(args)
        self.backbone = "monologg/kobert"
        self.tokenizer = KoBertTokenizer.from_pretrained(self.backbone)

    def _load_model(self):
        config = AutoConfig.from_pretrained(self.backbone)
        p_encoder = AutoEncoder(self.backbone, config=config).cuda()
        q_encoder = AutoEncoder(self.backbone, config=config).cuda()
        return p_encoder, q_encoder

    def _get_encoder(self):
        config = AutoConfig.from_pretrained(self.backbone)
        q_encoder = AutoEncoder(self.backbone, config=config).cuda()
        return q_encoder


class DprKorquadBertRetrieval(DprRetrieval):
    def __init__(self, args):
        super().__init__(args)
        self.backbone = "sangrimlee/bert-base-multilingual-cased-korquad"
        self.tokenizer = AutoTokenizer.from_pretrained(self.backbone)

    def _load_model(self):
        config = AutoConfig.from_pretrained(self.backbone)
        p_encoder = AutoEncoder(self.backbone, config=config).cuda()
        q_encoder = AutoEncoder(self.backbone, config=config).cuda()
        return p_encoder, q_encoder

    def _get_encoder(self):
        config = AutoConfig.from_pretrained(self.backbone)
        q_encoder = AutoEncoder(self.backbone, config=config).cuda()
        return q_encoder


class DprKoelectraRetrieval(DprRetrieval):
    def __init__(self, args):
        super().__init__(args)
        self.backbone = "monologg/koelectra-base-v3-finetuned-korquad"
        self.tokenizer = AutoTokenizer.from_pretrained(self.backbone)

    def _load_model(self):
        config = AutoConfig.from_pretrained(self.backbone)
        p_encoder = KoelectraEncoder(self.backbone, config=config).cuda()
        q_encoder = KoelectraEncoder(self.backbone, config=config).cuda()
        return p_encoder, q_encoder

    def _get_encoder(self):
        config = AutoConfig.from_pretrained(self.backbone)
        q_encoder = KoelectraEncoder(self.backbone, config=config).cuda()
        return q_encoder
