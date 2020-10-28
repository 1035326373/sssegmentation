'''
Function:
    test the model
Author:
    Zhenchao Jin
'''
import cv2
import copy
import torch
import pickle
import warnings
import argparse
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist
from modules import *
from tqdm import tqdm
from cfgs import BuildConfig
warnings.filterwarnings('ignore')


'''parse arguments in command line'''
def parseArgs():
    parser = argparse.ArgumentParser(description='sssegmentation is a general framework for our research on strongly supervised semantic segmentation')
    parser.add_argument('--modelname', dest='modelname', help='model you want to test', type=str, required=True)
    parser.add_argument('--datasetname', dest='datasetname', help='dataset for testing.', type=str, required=True)
    parser.add_argument('--local_rank', dest='local_rank', help='node rank for distributed testing', default=0, type=int)
    parser.add_argument('--nproc_per_node', dest='nproc_per_node', help='number of process per node', default=4, type=int)
    parser.add_argument('--backbonename', dest='backbonename', help='backbone network for testing.', type=str, required=True)
    parser.add_argument('--noeval', dest='noeval', help='set true if no ground truth could be used to eval the results.', type=bool)
    parser.add_argument('--checkpointspath', dest='checkpointspath', help='checkpoints you want to resume from.', type=str, required=True)
    args = parser.parse_args()
    return args


'''Tester'''
class Tester():
    def __init__(self, **kwargs):
        # set attribute
        for key, value in kwargs.items(): setattr(self, key, value)
        self.use_cuda = torch.cuda.is_available()
        # modify config for consistency
        if not self.use_cuda:
            if self.cmd_args.local_rank == 0: logger_handle.warning('Cuda is not available, only cpu is used to test the model...')
            self.cfg.MODEL_CFG['distributed']['is_on'] = False
            self.cfg.DATALOADER_CFG['test']['type'] = 'nondistributed'
        if self.cfg.MODEL_CFG['distributed']['is_on']:
            self.cfg.MODEL_CFG['is_multi_gpus'] = True
            self.cfg.DATALOADER_CFG['test']['type'] = 'distributed'
        # init distributed testing if necessary
        distributed_cfg = self.cfg.MODEL_CFG['distributed']
        if distributed_cfg['is_on']:
            dist.init_process_group(backend=distributed_cfg.get('backend', 'nccl'))
    '''start tester'''
    def start(self, all_preds, all_gts):
        cfg, logger_handle, use_cuda, cmd_args, cfg_file_path = self.cfg, self.logger_handle, self.use_cuda, self.cmd_args, self.cfg_file_path
        distributed_cfg, common_cfg = self.cfg.MODEL_CFG['distributed'], self.cfg.COMMON_CFG['train']
        # instanced dataset and dataloader
        dataset = BuildDataset(mode='TEST', logger_handle=logger_handle, dataset_cfg=copy.deepcopy(cfg.DATASET_CFG))
        assert dataset.num_classes == cfg.MODEL_CFG['num_classes'], 'parsed config file %s error...' % cfg_file_path
        dataloader_cfg = copy.deepcopy(cfg.DATALOADER_CFG)
        if distributed_cfg['is_on']:
            batch_size, num_workers = dataloader_cfg['test']['batch_size'], dataloader_cfg['test']['num_workers']
            batch_size //= self.ngpus_per_node
            num_workers //= self.ngpus_per_node
            assert batch_size * self.ngpus_per_node == dataloader_cfg['test']['batch_size'], 'unsuitable batch_size...'
            assert num_workers * self.ngpus_per_node == dataloader_cfg['test']['num_workers'], 'unsuitable num_workers...'
            dataloader_cfg['test'].update({'batch_size': batch_size, 'num_workers': num_workers})
        dataloader = BuildParallelDataloader(mode='TEST', dataset=dataset, cfg=dataloader_cfg)
        # instanced model
        cfg.MODEL_CFG['backbone']['pretrained'] = False
        model = BuildModel(model_type=cmd_args.modelname, cfg=copy.deepcopy(cfg.MODEL_CFG), mode='TEST')
        if distributed_cfg['is_on']:
            torch.cuda.set_device(cmd_args.local_rank)
            model.cuda(cmd_args.local_rank)
        else:
            if use_cuda: model = model.cuda()
        # load checkpoints
        checkpoints = loadcheckpoints(cmd_args.checkpointspath, logger_handle=logger_handle, cmd_args=cmd_args)
        model.load_state_dict(checkpoints['model'])
        # parallel
        if use_cuda and cfg.MODEL_CFG['is_multi_gpus']:
            model = BuildParallelModel(model, cfg.MODEL_CFG['distributed']['is_on'], device_ids=[cmd_args.local_rank])
            if ('syncbatchnorm' in cfg.MODEL_CFG['normlayer_opts']['type']) and (not cfg.MODEL_CFG['distributed']['is_on']):
                patch_replication_callback(model)
        # print config
        if cmd_args.local_rank == 0:
            logger_handle.info('Dataset used: %s, Number of images: %s' % (cmd_args.datasetname, len(dataset)))
            logger_handle.info('Model Used: %s, Backbone used: %s' % (cmd_args.modelname, cmd_args.backbonename))
            logger_handle.info('Checkpoints used: %s' % cmd_args.checkpointspath)
            logger_handle.info('Config file used: %s' % cfg_file_path)
        # set eval
        model.eval()
        # start to test
        FloatTensor = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor
        if hasattr(cfg, 'INFERENCE_CFG'):
            inference_cfg = copy.deepcopy(cfg.INFERENCE_CFG)
        else:
            inference_cfg = {'mode': 'whole', 'opts': {}}
        with torch.no_grad():
            if cfg.MODEL_CFG['distributed']['is_on']: dataloader.sampler.set_epoch(0)
            pbar = tqdm(enumerate(dataloader))
            for batch_idx, samples in pbar:
                if cmd_args.local_rank == 0: pbar.set_description('Processing %s/%s' % (batch_idx+1, len(dataloader)))
                images, widths, heights, gts = samples['image'], samples['width'], samples['height'], samples['groundtruth']
                outputs = self.inference(model, images.type(FloatTensor), inference_cfg, dataset.num_classes)                    
                for i in range(len(outputs)):
                    output = outputs[i].unsqueeze(0)
                    pred = F.interpolate(output, size=(heights[i], widths[i]), mode='bilinear', align_corners=model.align_corners)[0]
                    pred = (torch.argmax(pred, dim=0)).cpu().numpy().astype(np.int32)
                    all_preds.append(pred)
                    gt = gts[i].cpu().numpy().astype(np.int32)
                    gt[gt >= dataset.num_classes] = -1
                    all_gts.append(gt)
    '''inference'''
    def inference(self, model, images, inference_cfg, num_classes):
        assert inference_cfg['mode'] in ['whole', 'slide']
        if inference_cfg['mode'] == 'whole':
            outputs = model(images)
        else:
            opts = inference_cfg['opts']
            stride_h, stride_w = opts['stride']
            cropsize_h, cropsize_w = opts['cropsize']
            batch_size, _, image_h, image_w = images.size()
            num_grids_h = max(image_h - cropsize_h + stride_h - 1, 0) // stride_h + 1
            num_grids_w = max(image_w - cropsize_w + stride_w - 1, 0) // stride_w + 1
            outputs = images.new_zeros((batch_size, num_classes, image_h, image_w))
            count_mat = images.new_zeros((batch_size, 1, image_h, image_w))
            for h_idx in range(num_grids_h):
                for w_idx in range(num_grids_w):
                    x1, y1 = w_idx * stride_w, h_idx * stride_h
                    x2, y2 = min(x1 + cropsize_w, image_w), min(y1 + cropsize_h, image_h)
                    x1, y1 = max(x2 - cropsize_w, 0), max(y2 - cropsize_h, 0)
                    crop_images = images[:, :, y1:y2, x1:x2]
                    outputs_crop = model(crop_images)
                    outputs += F.pad(outputs_crop, (int(x1), int(outputs.shape[3] - x2), int(y1), int(outputs.shape[2] - y2)))
                    count_mat[:, :, y1:y2, x1:x2] += 1
            assert (count_mat == 0).sum() == 0
            outputs = outputs / count_mat
        return outputs


'''main'''
def main():
    # parse arguments, todo: support distributed testing and bs > 1
    args = parseArgs()
    cfg, cfg_file_path = BuildConfig(args.modelname, args.datasetname, args.backbonename)
    cfg.MODEL_CFG['distributed']['is_on'] = False
    cfg.MODEL_CFG['is_multi_gpus'] = False
    cfg.DATALOADER_CFG['test']['batch_size'] = 1
    # check backup dir
    common_cfg = cfg.COMMON_CFG['test']
    checkdir(common_cfg['backupdir'])
    # initialize logger_handle
    logger_handle = Logger(common_cfg['logfilepath'])
    # number of gpus
    ngpus_per_node = torch.cuda.device_count()
    if ngpus_per_node != args.nproc_per_node:
        if args.local_rank == 0: logger_handle.warning('ngpus_per_node is not equal to nproc_per_node...')
        ngpus_per_node = args.nproc_per_node
    # instanced Tester
    all_preds, all_gts = [], []
    client = Tester(cfg=cfg, ngpus_per_node=ngpus_per_node, logger_handle=logger_handle, cmd_args=args, cfg_file_path=cfg_file_path)
    client.start(all_preds, all_gts)
    # save results and evaluate
    if args.local_rank == 0: logger_handle.info('Finished, number of preds is %s and number of gts is %s...' % (len(all_preds), len(all_gts)))
    with open(common_cfg['resultsavepath'], 'wb') as fp:
        if args.local_rank == 0: pickle.dump([all_preds, all_gts], fp)
    if not args.noeval:
        dataset = BuildDataset(mode='TEST', logger_handle=logger_handle, dataset_cfg=copy.deepcopy(cfg.DATASET_CFG))
        result = dataset.evaluate(all_preds, all_gts)
        if args.local_rank == 0: logger_handle.info(result)


'''debug'''
if __name__ == '__main__':
    main()