import os.path as p
from itertools import product
from argparse import Namespace
from transformers import set_seed

from tools import update_args
from trainer_qa import QuestionAnsweringTrainer
from transformers import DataCollatorWithPadding
from prepare import prepare_dataset, preprocess_dataset, get_reader_model, compute_metrics


def train_reader(args):
    strategis = args.strategis
    seeds = args.seeds[: args.run_cnt]

    for idx, (seed, strategy) in enumerate(product(seeds, strategis)):
        args = update_args(args, strategy)  # auto add args.save_path, args.base_path
        args.strategy, args.seed = strategy, seed
        args.info = Namespace()
        set_seed(seed)
        print(strategy, seed)

        datasets = prepare_dataset(args, is_train=True)
        model, tokenizer = get_reader_model(args)

        train_dataset, post_processing_function = preprocess_dataset(args, datasets, tokenizer, is_train=True)
        eval_dataset, _ = preprocess_dataset(args, datasets, tokenizer, is_train=False)

        data_collator = DataCollatorWithPadding(tokenizer, pad_to_multiple_of=8 if args.train.fp16 else None)

        args.train.do_train = True
        args.train.do_eval = True

        # run_name: wandb run name
        # 테스트 해본 전략을 수정하고 다시 run 할 경우 덮어씌워집니다.
        # 새로운 전략(json)을 만드는 것을 추천합니다.

        args.train.run_name = "_".join([strategy, str(seed), args.alias])
        print(args.train.run_name)
        args.train.output_dir = p.join(args.path.checkpoint, args.train.run_name)
        print(args.train.output_dir)

        # TRAIN MRC
        trainer = QuestionAnsweringTrainer(
            model=model,
            args=args.train,  # training_args
            custom_args=args,
            train_dataset=train_dataset,
            eval_dataset=eval_dataset,
            eval_examples=datasets["validation"],
            tokenizer=tokenizer,
            data_collator=data_collator,
            post_process_function=post_processing_function,
            compute_metrics=compute_metrics,
        )

        trainer.train()


if __name__ == "__main__":
    from tools import get_args

    args = get_args()
    train_reader(args)