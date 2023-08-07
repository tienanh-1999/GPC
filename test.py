import argparse
import torch
import os
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'
from torch.nn import functional as nnf
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from torch.utils.data import DataLoader
from dataset import ImageCaptionDataset, prepare_colon, \
                    prepare_prostate_uhu_data, prepare_gastric, prepare_k19, prepare_colon_test_2, prepare_prostate_ubc_data
from model.model import ImageCaptionModel
from utils import save_config, generate, mapping_type_to_num
from torchvision.transforms import Resize
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data.distributed import DistributedSampler
from sklearn.metrics import precision_score, recall_score, f1_score, accuracy_score
from torch.optim.lr_scheduler import CosineAnnealingWarmRestarts


def prepare_for_testing(args):
    if args.dataset == 'kbsmc':
        _, _, test_set = prepare_colon()
    elif args.dataset == 'kbsmc_2':
       test_set = prepare_colon_test_2()
    elif args.dataset == 'uhu':
       _, _, test_set = prepare_prostate_uhu_data()
    elif args.dataset == 'ubc':
       test_set = prepare_prostate_ubc_data()
    elif args.dataset == 'gastric':
       _, _, test_set = prepare_gastric()
    elif args.dataset == 'k19':
       _, _, test_set = prepare_k19()

    model = ImageCaptionModel(args)
    test_dataset = ImageCaptionDataset(test_set, args.prefix_length, model.get_tokenizer())
    
    if args.pretrain_path is not None:
        checkpoint = torch.load(args.pretrain_path, map_location=args.device)
        try:
            model.load_state_dict(checkpoint['model_state_dict'])
        except:
            from collections import OrderedDict
            new_state_dict = OrderedDict()
            for k, v in checkpoint['model_state_dict'].items():
                name = k[7:] # remove `module.`
                new_state_dict[name] = v
            model.load_state_dict(new_state_dict)

    return model, test_dataset



def train(args, train_dataset, valid_dataset, model):
    batch_size = args.bs
    device = args.device
    epochs = args.epochs
    
    model.train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, betas=args.betas, weight_decay=args.weight_decay)
    train_sampler = DistributedSampler(train_dataset)
    valid_sampler = DistributedSampler(valid_dataset)

    train_dataloader = DataLoader(train_dataset, 
                                  batch_size=batch_size, 
                                  shuffle=False, 
                                  drop_last=True, 
                                  num_workers=0,
                                  sampler=train_sampler
                                )
    valid_dataloader = DataLoader(valid_dataset, 
                                  batch_size=batch_size, 
                                  shuffle=False, 
                                  drop_last=False, 
                                  num_workers=0,
                                  sampler=valid_sampler
                                )

    scheduler = CosineAnnealingWarmRestarts(optimizer, T_0=epochs//3, T_mult=1, eta_min=args.lr*0.1, last_epoch=-1)

    if torch.cuda.current_device() == 0:
        writer = SummaryWriter(args.out_dir)

    for epoch in range(epochs):
        train_sampler.set_epoch(epoch)
        valid_sampler.set_epoch(epoch)
        if torch.cuda.current_device() == 0:
            print(f">>> Training epoch {epoch}")
            progress = tqdm(total=len(train_dataloader), desc=args.prefix_outdir)

        total_train_loss = 0
        for _, (_, tokens, mask, img_tensor, _) in enumerate(train_dataloader):
            """
            - img_path: tuple, len = batch_size
            - tokens: tensor, shape (bs, max_token_len)  (padded tokens)
            - mask: tensor, shape (bs, max_token_len + prefix_len)
            - img_tensors: tensor, shape (bs, c, w, h)
            - caption: tuple, len = batch_size
            """
            model.train()
            model.zero_grad()
            tokens, mask, img_tensor = tokens.to(device), mask.to(device), img_tensor.to(device, dtype=torch.float32)
            if args.encoder in ['vit_b_16', 'clip']:
                img_tensor = Resize(224, antialias=None)(img_tensor)
            outputs = model(img_tensor, tokens, mask)
            logits = outputs.logits[:, train_dataset.prefix_length - 1: -1] # only get the logits excluding the prefix
            loss = nnf.cross_entropy(logits.reshape(-1, logits.shape[-1]), tokens.flatten(), ignore_index=0)
            # ignore the padding id, which is 0
            total_train_loss += loss.item()
            loss.backward()   # already synchronized gradient tensor
            optimizer.step()  # update model's weights based upon synced gradients (same update between processes)
            scheduler.step()
            optimizer.zero_grad()

            if torch.cuda.current_device() ==  0:
                progress.update()

        
        if torch.cuda.current_device() == 0:
            progress.close()

            torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'schedulerr_state_dict': scheduler.state_dict(),
                'loss': loss,
                }, 
                os.path.join(args.out_dir, f"{args.prefix_outdir}-{epoch}.pt"),
            )

        model.eval()

        ground_truth_list = []
        prediction_list = []

        if epoch % args.valid_every == 0:
            if torch.cuda.current_device() == 0:
                print(f">>> Evaluating epoch {epoch}")
                progress = tqdm(total=len(valid_dataloader), desc=args.prefix_outdir)
            total_valid_loss = 0
            with torch.no_grad():
                for _, (_, _, _, img_tensor, caption) in enumerate(valid_dataloader):
                    img_tensor = img_tensor.to(device, dtype=torch.float32)       # bs x 3 x 512 x 512
                    if args.encoder in ['vit_b_16', 'clip']:
                        img_tensor = Resize(224, antialias=None)(img_tensor)
                    gen_cap = generate(model, img_tensor, args=args)              
                    ground_truth_list += [mapping_type_to_num(cap) for cap in caption]
                    prediction_list += [mapping_type_to_num(pred) for pred in gen_cap]
                    if torch.cuda.current_device() == 0:
                        progress.update()
            if torch.cuda.current_device() == 0:
                progress.close()
        
        ground_truth_tensor = torch.tensor(ground_truth_list, device=args.device)
        prediction_tensor = torch.tensor(prediction_list, device=args.device)

        all_ground_truth = [torch.ones_like(ground_truth_tensor) for _ in range(dist.get_world_size())]
        all_prediction = [torch.ones_like(prediction_tensor) for _ in range(dist.get_world_size())]

        dist.barrier()

        dist.all_gather(all_ground_truth, ground_truth_tensor)
        dist.all_gather(all_prediction, prediction_tensor)

        for gt_list in all_ground_truth:
            ground_truth_list += gt_list.tolist()

        for p_list in all_prediction:
            prediction_list += p_list.tolist()

        if torch.cuda.current_device() == 0:
            if list(set(ground_truth_list)-set(prediction_list)) != []:
                print(set(ground_truth_list))
                print(set(prediction_list))
                print(list(set(ground_truth_list)-set(prediction_list)))
            valid_acc = accuracy_score(ground_truth_list, prediction_list)
            valid_f1 = f1_score(ground_truth_list, prediction_list, average='macro', labels=list(set(ground_truth_list)))
            valid_pre = precision_score(ground_truth_list, prediction_list, labels=list(set(ground_truth_list)), average='macro')
            valid_re = recall_score(ground_truth_list, prediction_list, labels=list(set(ground_truth_list)), average='macro')
            print(valid_pre, valid_acc, valid_f1, valid_re)
            
            writer.add_scalars('Loss', {'train_loss':total_train_loss/len(train_dataset), 
                                    'valid_acc':valid_acc, 
                                    'valid_f1':valid_f1,
                                    'valid_pre':valid_pre, 
                                    'valid_re':valid_re, 
                                    'valid_loss':total_valid_loss/len(valid_dataset)}, epoch)
            
    return model


def main_worker(gpu, ngpus_per_node):
    parser = argparse.ArgumentParser()

    # CHANGE
    parser.add_argument('--dataset', nargs='+', default=['kbsmc', 'uhu', 'gastric', 'k19'])
    parser.add_argument('--epochs', type=int, default=40)
    parser.add_argument('--bs', type=int, default=16)
    parser.add_argument('--prefix_outdir', type=str, default="")
    parser.add_argument('--device', type=int, default=gpu)        # DDP
    parser.add_argument('--world_size', type=int, default=ngpus_per_node)

    parser.add_argument('--encoder', type=str, default='convnext_large_v2')
    parser.add_argument('--lm', type=str, choices=['opt','gpt-2'], default='opt')
    parser.add_argument('--pretrain', type=str, default='facebook/opt-125m')

    # FIXED
    parser.add_argument('--out_dir', default='/data1/anhnguyen/image_caption/logs/ddp')
    parser.add_argument('--valid_every', type=int, default=1)
    parser.add_argument('--prefix_length', type=int, default=5)
    parser.add_argument('--lr', type=float, default=1e-6)
    parser.add_argument('--warmup_steps', type=int, default=5000)
    parser.add_argument('--betas', type=tuple, default=(0.9, 0.999))
    parser.add_argument('--weight_decay', type=float, default=0.01)
    parser.add_argument('--smooth', type=bool, default=False)
    parser.add_argument('--pretrain_path', type=str, default=None, help='Path of .pt file of pre-trained model')
    parser.add_argument('--mapping_type', type=str, choices='mlp', default='mlp')
    parser.add_argument('--freeze_lm', type=bool, default=True)

    
    args = parser.parse_args()

    embed_size = {
        "facebook/opt-125m": 768,
        "facebook/opt-350m": 512,
        "facebook/opt-1.3b": 2048,
        "gpt2": 768,
    }
    
    args.prefix_outdir = '-'.join((args.encoder, 
                                    args.mapping_type, 
                                    args.lm,
                                    args.pretrain.replace('/', '-'),
                                    ''.join(args.dataset), 
                                    str(args.prefix_length),
                                    args.prefix_outdir,
                                    'freeze_lm' if args.freeze_lm else 'unfreeze_lm'
                                    ))
    args.out_dir = args.out_dir + '/' + args.prefix_outdir 
    args.embedding_size = embed_size[args.pretrain]

    if args.device == 0:
        if not os.path.exists(args.out_dir):
            os.makedirs(args.out_dir)
        else:
            raise ValueError(f'The path already existed: {args.out_dir}')
    
        save_config(args)

    args.device = torch.device(f'cuda:{args.device}')
    
    train_set = []
    valid_set = []
    for dataset in args.dataset:
        if dataset == 'kbsmc':
            train_set_t, valid_set_t, _ = prepare_colon()
            train_set += train_set_t
            valid_set += valid_set_t

        elif dataset == 'uhu':
            train_set_t, valid_set_t, _ = prepare_prostate_uhu_data()
            train_set += train_set_t
            valid_set += valid_set_t
        
        elif dataset == 'gastric':
            train_set_t, valid_set_t, _ = prepare_gastric()
            train_set += train_set_t
            valid_set += valid_set_t
        
        elif dataset == 'k19':
            train_set_t, valid_set_t, _ = prepare_k19()
            train_set += train_set_t
            valid_set += valid_set_t
        
        else:
            raise ValueError(f'Invalid dataset: {dataset}')

    model = ImageCaptionModel(args)
    train_dataset = ImageCaptionDataset(train_set, args.prefix_length, model.get_tokenizer())
    valid_dataset = ImageCaptionDataset(valid_set, args.prefix_length, model.get_tokenizer())
    model = DDP(model, device_ids=[args.device])

    if args.pretrain_path is not None:
        model.load_state_dict(torch.load(args.pretrain_path)) 
    train(args, train_dataset, valid_dataset, model)

if __name__ == '__main__':
    #os.environ["CUDA_VISIBLE_DEVICES"]="5,6,7"
    ngpus_per_node = torch.cuda.device_count()
    mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node,))
