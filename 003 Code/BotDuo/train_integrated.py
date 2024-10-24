import os
import shutil
import argparse
import random
import timm
import wandb
import numpy as np
import pandas as pd
import albumentations as A
from glob import glob
from PIL import Image
from sklearn.model_selection import train_test_split
from albumentations.pytorch import ToTensorV2

import torch
import torch.nn as nn
import torch.optim as optim
from torchvision import transforms
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler

from arch.iformer.inception_transformer import iformer_small, iformer_base
from utils.datasets import StegDataset
from utils.utils import save_checkpoint

def worker_init_fn(worker_id):
    random.seed(args.seed + worker_id)
    np.random.seed(args.seed + worker_id)   

def train(train_loader, model, criterion, optimizer, device):
    model.train()
    total_loss = 0.0
    correct = 0
    for images, labels in train_loader:
        images, labels = images.to(device), labels.to(device)
        optimizer.zero_grad()
        outputs = model(images)
        loss = criterion(outputs, labels)
        loss.backward()
        optimizer.step()

        total_loss += loss.item()
        probs = torch.softmax(outputs.data, dim=1)
        _, predicted = torch.max(probs, 1)
        correct += (predicted == labels).sum().item()

    avg_loss = total_loss / len(train_loader)
    accuracy = 100 * correct / len(train_loader.dataset)
    
    return avg_loss, accuracy

def validate(valid_loader, model, criterion, device):
    model.eval()
    total_loss = 0.0
    correct = 0
    with torch.no_grad():
        for images, labels in valid_loader:
            images, labels = images.to(device), labels.to(device)
            outputs = model(images)
            loss = criterion(outputs, labels)

            total_loss += loss.item()
            probs = torch.softmax(outputs.data, dim=1)
            _, predicted = torch.max(probs, 1)
            correct += (predicted == labels).sum().item()

    avg_loss = total_loss / len(valid_loader)
    accuracy = 100 * correct / len(valid_loader.dataset)
    
    return avg_loss, accuracy

def main(args):
    if args.use_wandb:
        devices = ['Galaxy_Flip3', 'Galaxy_S20+', 'iPhone12_ProMax', 'Huawei_P30', 'LG_Wing']
        target_device = [device for device in devices if device not in args.train_devices]

        if args.run_name == 'auto':
            if len(args.train_devices) < 5:
                if args.stride is not None:
                    args.run_name = f'{args.backbone}_stride_{args.stride}_integrated_except_{"_".join(target_device)}_{args.stego_method}_{args.batch_size}_{args.lr}'
                else:
                    args.run_name = f'{args.backbone}_integrated_except_{"_".join(target_device)}_{args.stego_method}_{args.batch_size}_{args.lr}'
            else:
                if args.stride is not None:
                    args.run_name = f'{args.backbone}_integrated_{args.data_type}_{args.original_data+args.ipp_data+args.crawl_data}_stride_{args.stride}_{args.stego_method}_{args.batch_size}_{args.lr}'
                else:
                    args.run_name = f'{args.backbone}_integrated_{args.data_type}_{args.original_data+args.ipp_data+args.crawl_data}_{args.stego_method}_{args.batch_size}_{args.lr}'
            if args.suffix != '':
                args.run_name += f'_{args.suffix}'
        wandb.run.name = args.run_name

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if args.backbone.split('-')[0] == 'timm':
        model = timm.create_model(args.backbone.split('-')[1], pretrained=True, num_classes=2)
    elif args.backbone == 'iformer-small':
        model = iformer_small(pretrained=True)
        model.head = nn.Linear(model.head.in_features, 2)
    elif args.backbone == 'iformer-base':
        model = iformer_base(pretrained=True)
        model.head = nn.Linear(model.head.in_features, 2)

    if args.stride is not None:
        if args.backbone.split('-')[1] == 'mobilevit_s':
            model.stem.conv.stride = (args.stride, args.stride)
        elif args.backbone.split('-')[1] == 'efficientnet_b0':
            model.conv_stem.stride = (args.stride, args.stride)
        elif args.backbone.split('-')[0] == 'iformer':
            model.patch_embed.proj1.conv = (args.stride , args.stride)
        

    if args.pretrained_path is not None:
        state_dict = torch.load(args.pretrained_path)['state_dict']
        model.load_state_dict(state_dict)

    model = model.to(device)

    train_rate = float(args.train_rate)
    valid_rate = float(1-args.train_rate)
    original_train_num = int(args.original_data * train_rate) // len(args.train_devices)
    original_valid_num = int(args.original_data * valid_rate) // len(args.train_devices)
    if args.ipp_data > 0:
        ipp_train_num = int(args.ipp_data * train_rate) // len(args.train_devices)
        ipp_valid_num = int(args.ipp_data * valid_rate) // len(args.train_devices)
    if args.crawl_data > 0:
        crawl_train_num = int(args.crawl_data * train_rate) // len(args.crawl_platform)
        crawl_valid_num = int(args.crawl_data * valid_rate) // len(args.crawl_platform)

    train_df = pd.DataFrame()
    valid_df = pd.DataFrame()
    for train_device in args.train_devices:
        device_df = pd.read_csv(f'{args.csv_root}/single/{args.data_type}/{args.cover_size}_{args.stego_method}/{train_device}_train.csv')[:original_train_num]
        valid_df = pd.concat([valid_df, pd.read_csv(f'{args.csv_root}/single/{args.data_type}/{args.cover_size}_{args.stego_method}/{train_device}_valid.csv')[:original_valid_num]])
        
        if args.ipp_data > 0:
            ipp_df = pd.read_csv(f'{args.csv_root}/single/{args.data_type}/{args.cover_size}_ipp_{args.stego_method}/{train_device}_train.csv')[:ipp_train_num]
            train_df = pd.concat([train_df, device_df, ipp_df], ignore_index=True)
            valid_df = pd.concat([valid_df, pd.read_csv(f'{args.csv_root}/single/{args.data_type}/{args.cover_size}_ipp_{args.stego_method}/{train_device}_valid.csv')[:ipp_valid_num]])
        else:
            train_df = pd.concat([train_df, device_df], ignore_index=True)

    if args.crawl_data > 0:
        for crawl_platform in args.crawl_platform:
            crawl_df = pd.read_csv(f'{args.csv_root}/single/{args.data_type}/{args.cover_size}_{args.stego_method}/{crawl_platform}_train.csv')[:crawl_train_num]
            train_df = pd.concat([train_df, crawl_df], ignore_index=True)
            valid_df = pd.concat([valid_df, pd.read_csv(f'{args.csv_root}/single/{args.data_type}/{args.cover_size}_{args.stego_method}/{crawl_platform}_valid.csv')[:crawl_valid_num]])
    train_dataset = StegDataset(train_df, transform=train_transform)
    valid_dataset = StegDataset(valid_df, transform=test_transform)

    train_loader = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers_per_loader, shuffle=True, drop_last=True, worker_init_fn=worker_init_fn)
    valid_loader = DataLoader(valid_dataset, batch_size=args.batch_size, num_workers=args.workers_per_loader, shuffle=False, drop_last=False, worker_init_fn=worker_init_fn)

    criterion = nn.CrossEntropyLoss()
    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    if args.stride is not None:
        print(f'Model: {args.backbone} with First conv stride({args.stride}, {args.stride})')
    else:
        print(f'Model: {args.backbone}')
    print(f'Train Data: {len(train_loader.dataset)}')
    print(f'Valid Data: {len(valid_loader.dataset)}')
    print(f'=================================== integrated model training ===================================')
    best_valid_acc = 0.0
    for epoch in range(1, args.epochs+1):
        train_loss, train_acc = train(train_loader, model, criterion, optimizer, device)
        valid_loss, valid_acc = validate(valid_loader, model, criterion, device)
        print(f'Epoch [{epoch}/{args.epochs}], Train Loss: {train_loss:.4f}, Train Acc: {train_acc:.2f}, Valid Loss: {valid_loss:.4f}, Valid Acc: {valid_acc:.2f}')

        if args.use_wandb:
            wandb.log({'train_loss': train_loss, 'train_acc': train_acc, 'valid_loss': valid_loss, 'valid_acc': valid_acc}, step=epoch)
        is_best = valid_acc >= best_valid_acc
        if is_best:
            best_valid_acc = valid_acc
            if args.use_wandb:
                wandb.log({'best_valid_acc': best_valid_acc}, step=epoch)
                
            if args.save_model:
                save_checkpoint(
                    state={
                    'epoch': epoch,
                    'state_dict': model.state_dict(),
                    'best_valid_acc': best_valid_acc,
                    },
                    gpus=args.gpus,
                    is_best=is_best,
                    model_path=f'{args.ckpt_root}/{args.run_name}/',
                    model_name=f'ep{epoch}.pth.tar')
    wandb.finish()

if __name__ == '__main__':
    # Argument parsing
    parser = argparse.ArgumentParser(description='2024 NSR Steganlysis Training Parser')
    # Model Parsers
    parser.add_argument('--backbone', type=str, default='timm-efficientnet_b0', help='Backbone name from timm library')
    parser.add_argument('--pretrained_path', type=str, default=None, help='Path to pretrained model')
    parser.add_argument('--stride', type=int, default=None, help='Stride of the backbone')
    parser.add_argument('--dropout_rate', type=float, default=0.0, help='Dropout rate')
    parser.add_argument('--save_model', action='store_true', help='Whether to save the model')

    # Training Parsers
    parser.add_argument('--epochs', type=int, default=20, help='Number of epochs to train')
    parser.add_argument('--lr', type=float, default=1e-4, help='Learning rate')
    parser.add_argument('--batch_size', type=int, default=128, help='Batch size')
    parser.add_argument('--workers_per_loader', type=int, default=4, help='Number of workers per data loader')
    parser.add_argument('--weight_decay', type=float, default=1e-6, help='Weight decay for optimizer')
    parser.add_argument('--train_rate', type=float, default=0.7, help='Train split size')

    # MISC Parsers
    parser.add_argument('--csv_root', type=str, default='./csv/', help='Path to the csv')
    parser.add_argument('--gpus', type=str, default='0', help='Comma-separated list of GPU IDs to use')
    parser.add_argument('--seed', type=int, default=42, help='Seed for reproducibility') 
    parser.add_argument('--ckpt_root', type=str, default='./ckpt/', help='Root directory for checkpoint saving')
    parser.add_argument('--use_wandb', action='store_true', help='Whether to use wandb')
    parser.add_argument('--run_name', type=str, default='auto', help='Run name for checkpoint saving')
    parser.add_argument('--suffix', type=str, default='', help='Suffix for run name')
    
    parser.add_argument('--original_data', type=int, default=200000, help='Total number of data')
    parser.add_argument('--ipp_data', type=int, default=0, help='Total number of data')
    parser.add_argument('--crawl_data', type=int, default=0, help='Total number of data')
    parser.add_argument('--train_devices', type=str, nargs='+', default=['Galaxy_Flip3', 'Galaxy_S20+', 'iPhone12_ProMax', 'Huawei_P30', 'LG_Wing'])
    parser.add_argument('--crawl_platform', type=str, nargs='+', default=['Naver', 'Instagram'])
    parser.add_argument('--data_type', type=str, default='JPEG', choices=['PNG', 'JPEG'])
    parser.add_argument('--cover_size', type=str, default='224')
    parser.add_argument('--stego_method', type=str, default='nsf5_0.5')
    args = parser.parse_args()

    if args.gpus != '':
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpus

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    random.seed(args.seed)
    np.random.seed(args.seed)
    os.environ['PYTHONHASHSEED'] = str(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    train_transform = A.Compose([
        A.CenterCrop(height=224, width=224),
        A.HorizontalFlip(p=0.5),
        A.VerticalFlip(p=0.5),
        A.Normalize(
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225],
        ),
        ToTensorV2(),
    ])
    
    test_transform = A.Compose([
        A.CenterCrop(height=224, width=224),
        A.Normalize(
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225],
        ),
        ToTensorV2(),
    ])

    if args.use_wandb:
        wandb.init(project='2024_NSR_Steganalysis', entity='kumdingso')
        wandb.save(f'./utils/datasets.py', policy='now')
        wandb.save(f'./train_integrated.py', policy='now')
        for arg in vars(args):
            wandb.config[arg] = getattr(args, arg)
    main(args)
