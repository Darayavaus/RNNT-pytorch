from models.models import Transducer
from data.data_loader import AudioDataLoader, SpectrogramDataset, BucketingSampler, DistributedBucketingSampler
import argparse
import json
import os
import time
import codecs
import torch.distributed as dist
import torch.utils.data.distributed
import models.eval_utils as eval_utils

# parameter setting
parser = argparse.ArgumentParser(description='RNN-T training')
parser.add_argument('--train-manifest', metavar='DIR',
                    help='path to train manifest csv', default='data/train_manifest.csv')
parser.add_argument('--val-manifest', metavar='DIR',
                    help='path to validation manifest csv', default='data/val_manifest.csv')
parser.add_argument('--sample-rate', default=16000, type=int, help='Sample rate')
parser.add_argument('--batch-size', default=10, type=int, help='Batch size for training')
parser.add_argument('--dropout', default=0.5, type=float, help='Dropout size for training')
parser.add_argument('--num-workers', default=6, type=int, help='Number of workers used in data-loading')
parser.add_argument('--labels-path', default='labels_eng.json', help='Contains all characters for transcription')
parser.add_argument('--window-size', default=.02, type=float, help='Window size for spectrogram in seconds')
parser.add_argument('--window-stride', default=.01, type=float, help='Window stride for spectrogram in seconds')
parser.add_argument('--window', default='hamming', help='Window type for spectrogram generation')
parser.add_argument('--epochs', default=1000, type=int, help='Number of training epochs')
parser.add_argument('--cuda', dest='cuda', action='store_true', help='Use cuda to train model')
parser.add_argument('--lr', '--learning-rate', default=1e-3, type=float, help='initial learning rate')
parser.add_argument('--tensorboard',default=True, help='Turn on tensorboard graphing')
parser.add_argument('--log-dir', default='logs/', help='Location of tensorboard log')
parser.add_argument('--id', default='RNNT training', help='Identifier for tensorboard run')
parser.add_argument('--save-folder', default='models/', help='Location to save epoch models')
parser.add_argument('--model-path', default='models/RNNT_model.pth',
                    help='Location to save best validation model')
parser.add_argument('--continue-from', default='', help='Continue from checkpoint model')
parser.add_argument('--augment', dest='augment', action='store_true', help='Use random tempo and gain perturbations.')
parser.add_argument('--noise-dir', default=None,
                    help='Directory to inject noise into audio. If default, noise Inject not added')
parser.add_argument('--noise-prob', default=0.4, help='Probability of noise being added per sample')
parser.add_argument('--noise-min', default=0.0,
                    help='Minimum noise level to sample from. (1.0 means all noise, not original signal)', type=float)
parser.add_argument('--noise-max', default=0.5,
                    help='Maximum noise levels to sample from. Maximum 1.0', type=float)
parser.add_argument('--world-size', default=1, type=int,
                    help='number of distributed processes')
parser.add_argument('--rank', default=0, type=int,
                    help='The rank of this process')
parser.add_argument('--gpu-rank', default=None,
                    help='If using distributed parallel for multi-gpu, sets the GPU for the process')
parser.add_argument('--spec-augment', dest='spec_augment', action='store_true', help='using SpecAugment')

# setting seed
torch.manual_seed(72160258)
torch.cuda.manual_seed_all(72160258)


def to_np(x):
    return x.data.cpu().numpy()

if __name__ == '__main__':
    args = parser.parse_args()
    args.distributed = args.world_size > 1
    main_proc = True

    # ==========================================
    # PREPROCESS
    # ==========================================

    # Device configuration
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    if args.distributed:
        if args.gpu_rank:
            torch.cuda.set_device(int(args.gpu_rank))
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)
        main_proc = args.rank == 0  # Only the first proc should save models
    else:
        if args.cuda and args.gpu_rank:
            torch.cuda.set_device(int(args.gpu_rank))

    save_folder = args.save_folder

    loss_results, cer_results, wer_results = torch.Tensor(args.epochs), torch.Tensor(args.epochs), torch.Tensor(args.epochs)
    best_wer = None

    # visualization setting
    if args.tensorboard:
        print("visualizing by tensorboard")
        os.makedirs(args.log_dir, exist_ok=True)
        from tensorboardX import SummaryWriter
        tensorboard_writer = SummaryWriter(args.log_dir)
    os.makedirs(save_folder, exist_ok=True)

    save_folder = args.save_folder

    avg_loss, start_epoch, start_iter = 0, 0, 0

    args = parser.parse_args()
    args.distributed = args.world_size > 1
    main_proc = True

    if args.distributed:
        if args.gpu_rank:
            torch.cuda.set_device(int(args.gpu_rank))
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)
        main_proc = args.rank == 0  # Only the first proc should save models
    else:
        if args.cuda and args.gpu_rank:
            torch.cuda.set_device(int(args.gpu_rank))

    # ==========================================
    # DATA SET
    # ==========================================

    # setting dataset, data_loader
    audio_conf = dict(sample_rate=args.sample_rate,
                      window_size=args.window_size,
                      window_stride=args.window_stride,
                      window=args.window,
                      noise_dir=args.noise_dir,
                      noise_prob=args.noise_prob,
                      noise_levels=(args.noise_min, args.noise_max))

    with codecs.open(args.labels_path, 'r', encoding='utf-8') as label_file:
        labels = str(''.join(json.load(label_file)))

    train_dataset = SpectrogramDataset(audio_conf=audio_conf, manifest_filepath=args.train_manifest, labels=labels,
                                       normalize=True, augment=False, specaugment=False)
    test_dataset = SpectrogramDataset(audio_conf=audio_conf, manifest_filepath=args.val_manifest, labels=labels,
                                      normalize=True, augment=False, specaugment=False)

    if not args.distributed:
        train_sampler = BucketingSampler(train_dataset, batch_size=args.batch_size)
    else:
        train_sampler = DistributedBucketingSampler(train_dataset, batch_size=args.batch_size,
                                                    num_replicas=args.world_size, rank=args.rank)

    train_loader = AudioDataLoader(train_dataset,
                                   num_workers=args.num_workers, batch_sampler=train_sampler)
    test_loader = AudioDataLoader(test_dataset, batch_size=args.batch_size, num_workers=args.num_workers)

    # ==========================================
    # NETWORK SETTING
    # ==========================================
    # load model
    test_model = Transducer(input_size=161,
                            vocab_size=len(labels),
                            hidden_size=128,
                            num_layers=2,
                            dropout=args.dropout,
                            bidirectional=True).to(device)

    test_optimizer = torch.optim.SGD(filter(lambda p: p.requires_grad, test_model.parameters()),
                                     lr=args.lr, momentum=.9)
    # test_optimizer = torch.optim.Adam()
    print(test_model)
    # ==========================================
    # TRAINING
    # ==========================================
    for step in range(args.epochs):

        total_loss = 0
        totloss = 0; losses = []; test_losses=[];
        start_time = time.time()

        for i, (data) in enumerate(train_loader):

            if i == len(train_sampler):
                break

            inputs, targets, input_percentages, target_sizes, targets_one_hot, targets_list, labels_map = data

            input_sizes = input_percentages.mul_(int(inputs.size(3))).int()

            if inputs.shape[0] == 1:
                break

            inputs = inputs.to(device)
            targets_list = targets_list.to(device)

            test_model.train()
            test_optimizer.zero_grad()
            test_loss = test_model(inputs, targets_list, input_sizes, target_sizes)
            test_loss.backward()
            test_optimizer.step()
            test_losses.append(test_loss)

            if i % 10 == 0 and i > 0:
                temp_losses = total_loss / 10
                # print('[Epoch %d Batch %d] loss %.2f' %(step, i, temp_losses))
                total_loss = 0

        eval_losses =[]

        # ==========================================
        # EVALUATION
        # ==========================================

        for i, (data) in enumerate(test_loader):

            inputs, targets, input_percentages, target_sizes, targets_one_hot, targets_list, labels_map = data

            input_sizes = input_percentages.mul_(int(inputs.size(3))).int()

            if inputs.shape[0] == 1:
                break

            inputs = inputs.to(device)
            targets_list = targets_list.to(device)

            eval_loss = test_model(inputs, targets_list, input_sizes, target_sizes)

            inverse_map = dict((v, k) for k, v in labels_map.items())

            # y, nll = test_model.beam_search(inputs, labels_map=labels_map)
            y, nll = test_model.greedy_decode(inputs)
            y_mapped = [inverse_map[i] for i in y]
            print("===========inference & RAW =============")
            print(y_mapped)
            targets_list = targets_list.tolist()
            # for i in range(len(targets_list)):
            #     print([inverse_map[j] for j in targets_list[i]])
            print([inverse_map[j] for j in targets_list[1]])
            eval_losses.append(eval_loss)

            ## TO DO eval using metric WER, CER
            # eval_utils.compute_cer()

        losses = sum(losses) / len(train_loader)
        test_losses = sum(test_losses) / len(train_loader)
        eval_losses = sum(eval_losses) / len(test_loader)
        total_loss = 0

        print('[Epoch %d ] loss %.2f, eval loss %2.f' %(step, test_losses, eval_losses))

        if step % 50 == 0:
            temp_model_name = "logs/" + str(step) + "_an4_rnnt_model.pt"
            torch.save(test_model, temp_model_name)
