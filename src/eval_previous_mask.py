import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from args import get_parser
from utils.utils import batch_to_var, batch_to_var_test, make_dir, outs_perms_to_cpu, load_checkpoint, check_parallel
from modules.model import RSISMask, FeatureExtractor
from test import test, test_prev_mask
from dataloader.dataset_utils import sequence_palette
from PIL import Image
from scipy.misc import imread
from scipy.misc import imsave
from scipy.misc import imresize
from scipy.misc import toimage
# import scipy
from dataloader.dataset_utils import get_dataset
import torch
import numpy as np
from torchvision import transforms
import torch.utils.data as data
import sys, os
import json
from torch.autograd import Variable
import time
import os.path as osp
import cv2


class Evaluate():

    def __init__(self, args):

        self.split = args.eval_split
        self.dataset = args.dataset
        to_tensor = transforms.ToTensor()
        normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                         std=[0.229, 0.224, 0.225])

        image_transforms = transforms.Compose([to_tensor, normalize])

        if args.dataset == 'davis2017':
            dataset = get_dataset(args,
                                  split=self.split,
                                  e=0,
                                  image_transforms=image_transforms,
                                  target_transforms=None,
                                  augment=args.augment and self.split == 'train',
                                  inputRes=(240, 427),
                                  video_mode=True,
                                  use_prev_mask=True,
                                  eval=True)
        else:  # args.dataset == 'youtube' or kittimots
            dataset = get_dataset(args,
                                  split=self.split,
                                  e=0,
                                  image_transforms=image_transforms,
                                  target_transforms=None,
                                  augment=args.augment and self.split == 'train',
                                  #inputRes=(256, 448),
                                  inputRes=(287, 950),
                                  #inputRes=(412,723),
                                  #inputRes=(178,590),
                                  video_mode=True,
                                  use_prev_mask=True,
                                  eval=True)

        self.loader = data.DataLoader(dataset, batch_size=args.batch_size,
                                      shuffle=False,
                                      num_workers=args.num_workers,
                                      drop_last=False)

        self.args = args

        print(args.model_name)
        encoder_dict, decoder_dict, _, _, load_args = load_checkpoint(args.model_name, args.use_gpu)
        load_args.use_gpu = args.use_gpu
        self.encoder = FeatureExtractor(load_args)
        self.decoder = RSISMask(load_args)

        print(load_args)

        if args.ngpus > 1 and args.use_gpu:
            self.decoder = torch.nn.DataParallel(self.decoder, device_ids=range(args.ngpus))
            self.encoder = torch.nn.DataParallel(self.encoder, device_ids=range(args.ngpus))

        encoder_dict, decoder_dict = check_parallel(encoder_dict, decoder_dict)
        self.encoder.load_state_dict(encoder_dict)

        to_be_deleted_dec = []
        for k in decoder_dict.keys():
            if 'fc_stop' in k:
                to_be_deleted_dec.append(k)
        for k in to_be_deleted_dec:
            del decoder_dict[k]
        self.decoder.load_state_dict(decoder_dict)

        if args.use_gpu:
            self.encoder.cuda()
            self.decoder.cuda()

        self.encoder.eval()
        self.decoder.eval()
        if load_args.length_clip == 1:
            self.video_mode = False
            print('video mode not activated')
        else:
            self.video_mode = True
            print('video mode activated')

    def run_eval(self):
        print("Dataset is %s" % (self.dataset))
        print("Split is %s" % (self.split))

        if args.overlay_masks:

            colors = []
            palette = sequence_palette()
            inv_palette = {}
            for k, v in palette.items():
                inv_palette[v] = k
            num_colors = len(inv_palette.keys())
            for id_color in range(num_colors):
                if id_color == 0 or id_color == 21:
                    continue
                c = inv_palette[id_color]
                colors.append(c)

        if self.split == 'val-inference':

            if args.dataset == 'youtube':

                masks_sep_dir = os.path.join('../models', args.model_name, 'masks_sep_2assess')
                make_dir(masks_sep_dir)
                if args.overlay_masks:
                    results_dir = os.path.join('../models', args.model_name, 'results')
                    make_dir(results_dir)

                json_data = open('../../databases/YouTubeVOS/train/train-val-meta.json')
                data = json.load(json_data)

            elif args.dataset == 'davis2017':

                import lmdb
                from misc.config import cfg

                masks_sep_dir = os.path.join('../models', args.model_name, 'masks_sep_2assess-davis')
                make_dir(masks_sep_dir)

                if args.overlay_masks:
                    results_dir = os.path.join('../models', args.model_name, 'results-davis')
                    make_dir(results_dir)

                lmdb_env_seq_dir = osp.join(cfg.PATH.DATA, 'lmdb_seq')

                if osp.isdir(lmdb_env_seq_dir):
                    lmdb_env_seq = lmdb.open(lmdb_env_seq_dir)
                else:
                    lmdb_env_seq = None
            else:

                import lmdb
                from misc.config_kittimots import cfg

                masks_sep_dir = os.path.join('../models', args.model_name, 'masks_sep_2assess-kitti')
                make_dir(masks_sep_dir)

                if args.overlay_masks:
                    results_dir = os.path.join('../models', args.model_name, 'results-kitti')
                    make_dir(results_dir)

                lmdb_env_seq_dir = osp.join(cfg.PATH.DATA, 'lmdb_seq')

                if osp.isdir(lmdb_env_seq_dir):
                    lmdb_env_seq = lmdb.open(lmdb_env_seq_dir)
                else:
                    lmdb_env_seq = None

            for batch_idx, (inputs, targets, seq_name, starting_frame, frames_with_new_ids) in enumerate(self.loader):

                prev_hidden_temporal_list = None
                max_ii = min(len(inputs), args.length_clip)
                frames_with_new_ids = np.array(frames_with_new_ids)
                #print('Variable max_ii')
                #print(max_ii)

                if args.overlay_masks:
                    base_dir = results_dir + '/' + seq_name[0] + '/'
                    make_dir(base_dir)

                if args.dataset == 'davis2017':
                    key_db = osp.basename(seq_name[0])

                    if not lmdb_env_seq == None:
                        with lmdb_env_seq.begin() as txn:
                            _files_vec = txn.get(key_db.encode()).decode().split('|')
                            _files = [osp.splitext(f)[0] for f in _files_vec]
                    else:
                        seq_dir = osp.join(cfg['PATH']['SEQUENCES'], key_db)
                        _files_vec = os.listdir(seq_dir)
                        _files = [osp.splitext(f)[0] for f in _files_vec]

                    frame_names = sorted(_files)

                if args.dataset == 'kittimots':
                    key_db = osp.basename(seq_name[0])

                    if not lmdb_env_seq == None:
                        with lmdb_env_seq.begin() as txn:
                            _files_vec = txn.get(key_db.encode()).decode().split('|')
                            _files = [osp.splitext(f)[0] for f in _files_vec]
                    else:
                        seq_dir = osp.join(cfg['PATH']['SEQUENCES'], key_db)
                        _files_vec = os.listdir(seq_dir)
                        _files = [osp.splitext(f)[0] for f in _files_vec]

                    frame_names = sorted(_files)  # llistat de frames d'una seqüència de video

                dict_outs = {}
                #print("NEW OBJECTS FRAMES", frames_with_new_ids)

                # make a dir of results for each instance
                '''for t in range(args.maxseqlen):
                    base_dir_2 = results_dir + '/' + seq_name[0] + '/' + str(t)
                    make_dir(base_dir_2)'''

                for ii in range(max_ii):  # iteració sobre els frames/clips amb dimensio lenght_clip

                    # start_time = time.time()
                    #                x: input images (N consecutive frames from M different sequences)
                    #                y_mask: ground truth annotations (some of them are zeros to have a fixed length in number of object instances)
                    #                sw_mask: this mask indicates which masks from y_mask are valid
                    x, y_mask, sw_mask = batch_to_var(args, inputs[ii], targets[ii])

                    #print(seq_name[0] + '/' + frame_names[ii])

                    if ii == 0:
                        #one-shot approach, information about the first frame of the sequende
                        prev_mask = y_mask

                        #list of the first instances that appear on the sequence and update of the dictionary
                        annotation = Image.open(
                            '../../databases/KITTIMOTS/Annotations/' + seq_name[0] + '/' + frame_names[
                                ii] + '.png').convert('P')
                        annot = np.expand_dims(annotation, axis=0)
                        annot = torch.from_numpy(annot)
                        annot = annot.float()
                        instance_ids = np.unique(annot)
                        for i in instance_ids[1:]:
                            dict_outs.update({str(int(i-1)):int(i)})
                        #instances = len(instance_ids)-1


                    #one-shot approach, add GT information when a new instance appears on the video sequence
                    if args.dataset == 'kittimots':
                        if ii > 0 and ii in frames_with_new_ids:
                            frame_name = frame_names[ii]
                            annotation = Image.open(
                                '../../databases/KITTIMOTS/Annotations/' + seq_name[
                                    0] + '/' + frame_name + '.png').convert('P')

                            #annot = imresize(annotation, (256, 448), interp='nearest')
                            annot = imresize(annotation, (287,950), interp='nearest')
                            #annot = imresize(annotation, (412, 723), interp='nearest')
                            #annot = imresize(annotation, (178, 590), interp='nearest')
                            annot = np.expand_dims(annot, axis=0)
                            annot = torch.from_numpy(annot)
                            annot = annot.float()
                            annot = annot.numpy().squeeze()

                            new_instance_ids = np.setdiff1d(np.unique(annot), instance_ids)
                            annot = annot_from_mask(annot, new_instance_ids)
                            annot = np.expand_dims(annot, axis=0)
                            annot = torch.from_numpy(annot)
                            annot = Variable(annot.float(), requires_grad=False)
                            annot = annot.cuda()
                            #adding only the information of the new instance after the last active branch
                            for kk in new_instance_ids:
                                if dict_outs:
                                    last = int(list(dict_outs.keys())[-1])
                                else:
                                    #if the dictionary is empty
                                    last = -1
                                prev_mask[:, int(last+1), :] = annot[:, int(kk - 1), :]
                                dict_outs.update({str(last+1):int(kk)})
                            del annot
                            #update the list of instances that have appeared on the video sequence
                            if len(new_instance_ids) > 0:
                                instance_ids = np.append(instance_ids, new_instance_ids)
                            #instances = instances + len(new_instance_ids)

                    # from one frame to the following frame the prev_hidden_temporal_list is updated.
                    outs, hidden_temporal_list = test_prev_mask(args, self.encoder, self.decoder, x,
                                                                prev_hidden_temporal_list, prev_mask)

                    # end_inference_time = time.time()
                    # print("inference time: %.3f" %(end_inference_time-start_time))


                    if args.dataset == 'youtube':
                        num_instances = len(data['videos'][seq_name[0]]['objects'])
                    else:
                        num_instances = int(torch.sum(sw_mask.data).data.cpu().numpy())
                    num_instances = args.maxseqlen

                    base_dir_masks_sep = masks_sep_dir + '/' + seq_name[0] + '/'
                    make_dir(base_dir_masks_sep)

                    x_tmp = x.data.cpu().numpy()
                    height = x_tmp.shape[-2]
                    width = x_tmp.shape[-1]

                    '''out_stop = outs[1]
                    outs = outs[0]
                    for m in range(len(out_stop[0])):
                            print(m)
                            print(out_stop[0][m])'''


                    #print("OUT STOP: ", out_stop[0])

                    outs_masks = np.zeros((args.maxseqlen,), dtype=int)

                    for t in range(num_instances):

                        mask_pred = (torch.squeeze(outs[0, t, :])).cpu().numpy()
                        mask_pred = np.reshape(mask_pred, (height, width))
                        indxs_instance = np.where(mask_pred > 0.5)
                        '''indxs_instance = np.where((0.6 > mask_pred) & (mask_pred>= 0.5))
                        indxs_instance_1 = np.where((0.7 > mask_pred) & (mask_pred>= 0.6))
                        indxs_instance_2 = np.where((0.8 > mask_pred) & (mask_pred>= 0.7))
                        indxs_instance_3 = np.where((0.9 > mask_pred) & (mask_pred>= 0.8))
                        indxs_instance_4 = np.where((0.999> mask_pred) & (mask_pred>= 0.9))
                        indxs_instance_5 = np.where((0.9999 > mask_pred) & (mask_pred>= 0.999))'''



                        mask2assess = np.zeros((height, width))
                        mask2assess[indxs_instance] = 255

                        ''' mask2assess[indxs_instance] = 40
                        mask2assess[indxs_instance_1] = 80
                        mask2assess[indxs_instance_2] = 120
                        mask2assess[indxs_instance_3] = 160
                        mask2assess[indxs_instance_4] = 200
                        mask2assess[indxs_instance_5] = 255'''


                        if str(t) in dict_outs:
                            i = dict_outs[str(t)]
                        else:
                            break

                        if args.dataset == 'youtube':
                            toimage(mask2assess, cmin=0, cmax=255).save(
                                base_dir_masks_sep + '%05d_instance_%02d.png' % (starting_frame[0] + ii, i))
                        else:
                            toimage(mask2assess, cmin=0, cmax=255).save(
                                base_dir_masks_sep + frame_names[ii] + '_instance_%02d.png' % (i))
                        #create vector of predictions, gives information about which branches are active
                        if len(indxs_instance[0]) != 0:
                            outs_masks[t] = 1
                        else:
                            outs_masks[t] = 0

                    outs = outs.cpu().numpy()
                    #print("INS: ", outs_masks)
                    #print(json.dumps(dict_outs))

                    #delete spurious branches
                    last_position = last_ocurrence(outs_masks, 1) + 1
                    while len(dict_outs) < last_position:
                        for n in range(args.maxseqlen):
                            if outs_masks[n] == 1 and str(n) not in dict_outs:
                                outs = np.delete(outs, n, axis=1)
                                outs_masks = np.delete(outs_masks, n)
                                del hidden_temporal_list[n]
                                z = np.zeros((height * width))
                                outs = np.insert(outs, args.maxseqlen - 1, z, axis=1)
                                hidden_temporal_list.append(None)
                                outs_masks = np.append(outs_masks, 0)
                        last_position = last_ocurrence(outs_masks, 1) + 1

                    instances = sum(outs_masks)  # number of active branches
                    #delete branches of instances that disappear and rearrange
                    for n in range(args.maxseqlen):

                        while outs_masks[n] == 0 and n < instances:

                            outs = np.delete(outs, n, axis=1 )
                            outs_masks = np.delete(outs_masks, n)
                            del hidden_temporal_list[n]
                            z = np.zeros((height * width))
                            outs = np.insert(outs, args.maxseqlen-1, z, axis=1)
                            hidden_temporal_list.append(None)
                            outs_masks = np.append(outs_masks, 0)


                            #update dictionary by shifting entries
                            for m in range(len(dict_outs)-(n+1)):
                                value = dict_outs[str(m + n + 1)]
                                dict_outs.update({str(n + m): value})
                            #print(json.dumps(dict_outs))
                            last = int(list(dict_outs.keys())[-1])
                            del dict_outs[str(last)]

                    #an instance has disappeared, update dictionary
                    while len(dict_outs) > sum(outs_masks):
                        last = int(list(dict_outs.keys())[-1])
                        del dict_outs[str(last)]

                    outs = torch.from_numpy(outs)
                    outs = outs.cuda()
                    #print("OUTS: ", outs_masks)
                    #print(json.dumps(dict_outs))



                    # end_saving_masks_time = time.time()
                    # print("inference + saving masks time: %.3f" %(end_saving_masks_time - start_time))
                    if args.dataset == 'youtube':
                        print(seq_name[0] + '/' + '%05d' % (starting_frame[0] + ii))
                    else:
                        print(seq_name[0] + '/' + frame_names[ii])

                    if args.overlay_masks:

                        frame_img = x.data.cpu().numpy()[0, :, :, :].squeeze()
                        frame_img = np.transpose(frame_img, (1, 2, 0))
                        mean = np.array([0.485, 0.456, 0.406])
                        std = np.array([0.229, 0.224, 0.225])
                        frame_img = std * frame_img + mean
                        frame_img = np.clip(frame_img, 0, 1)
                        plt.figure();
                        plt.axis('off')
                        plt.figure();
                        plt.axis('off')
                        plt.imshow(frame_img)
                        #print("INSTANCES: ", instances)

                        for t in range(instances):

                            mask_pred = (torch.squeeze(outs[0, t, :])).cpu().numpy()
                            mask_pred = np.reshape(mask_pred, (height, width))
                            ax = plt.gca()
                            tmp_img = np.ones((mask_pred.shape[0], mask_pred.shape[1], 3))
                            if str(t) in dict_outs:
                                color_mask = np.array(colors[dict_outs[str(t)]]) / 255.0
                            else:
                                #color_mask = np.array(colors[0]) / 255.0
                                break
                            for i in range(3):
                                tmp_img[:, :, i] = color_mask[i]
                            ax.imshow(np.dstack((tmp_img, mask_pred * 0.7)))

                        if args.dataset == 'youtube':
                            figname = base_dir + 'frame_%02d.png' % (starting_frame[0] + ii)
                        else:
                            figname = base_dir + frame_names[ii] + '.png'

                        plt.savefig(figname, bbox_inches='tight')
                        plt.close()

                    #Print a video for each instance
                    '''for t in range(instances):

                        if args.overlay_masks:

                            frame_img = x.data.cpu().numpy()[0, :, :, :].squeeze()
                            frame_img = np.transpose(frame_img, (1, 2, 0))
                            mean = np.array([0.485, 0.456, 0.406])
                            std = np.array([0.229, 0.224, 0.225])
                            frame_img = std * frame_img + mean
                            frame_img = np.clip(frame_img, 0, 1)
                            plt.figure();
                            plt.axis('off')
                            plt.figure();
                            plt.axis('off')
                            plt.imshow(frame_img)


                            mask_pred = (torch.squeeze(outs[0, t, :])).cpu().numpy()
                            mask_pred = np.reshape(mask_pred, (height, width))
                            ax = plt.gca()
                            tmp_img = np.ones((mask_pred.shape[0], mask_pred.shape[1], 3))
                            if str(t) in dict_outs:
                                color_mask = np.array(colors[dict_outs[str(t)]]) / 255.0
                            else:
                                #color_mask = np.array(colors[0]) / 255.0
                                break
                            for i in range(3):
                                tmp_img[:, :, i] = color_mask[i]
                            ax.imshow(np.dstack((tmp_img, mask_pred * 0.7)))

                            figname = base_dir + '/' + str(dict_outs[str(t)]) + '/' + frame_names[ii] + '.png'

                            plt.savefig(figname, bbox_inches='tight')
                            plt.close()'''

                    if self.video_mode:
                        if args.only_spatial == False:
                            prev_hidden_temporal_list = hidden_temporal_list
                        if ii > 0:
                            prev_mask = outs
                        else:
                            prev_mask = y_mask

                    del outs, hidden_temporal_list, x, y_mask, sw_mask

        else:

            if args.dataset == 'youtube':

                masks_sep_dir = os.path.join('../models', args.model_name, 'masks_sep_2assess_val')
                make_dir(masks_sep_dir)
                if args.overlay_masks:
                    results_dir = os.path.join('../models', args.model_name, 'results_val')
                    make_dir(results_dir)

                json_data = open('../../databases/YouTubeVOS/val/meta.json')
                data = json.load(json_data)

            elif args.dataset == 'davis2017':

                import lmdb
                from misc.config import cfg

                masks_sep_dir = os.path.join('../models', args.model_name, 'masks_sep_2assess_val_davis')
                make_dir(masks_sep_dir)
                if args.overlay_masks:
                    results_dir = os.path.join('../models', args.model_name, 'results_val_davis')
                    make_dir(results_dir)

                lmdb_env_seq_dir = osp.join(cfg.PATH.DATA, 'lmdb_seq')

                if osp.isdir(lmdb_env_seq_dir):
                    lmdb_env_seq = lmdb.open(lmdb_env_seq_dir)
                else:
                    lmdb_env_seq = None
            else:

                import lmdb
                from misc.config_kittimots import cfg

                masks_sep_dir = os.path.join('../models', args.model_name, 'masks_sep_2assess-kitti')
                make_dir(masks_sep_dir)

                if args.overlay_masks:
                    results_dir = os.path.join('../models', args.model_name, 'results-kitti')
                    make_dir(results_dir)

                lmdb_env_seq_dir = osp.join(cfg.PATH.DATA, 'lmdb_seq')

                if osp.isdir(lmdb_env_seq_dir):
                    lmdb_env_seq = lmdb.open(lmdb_env_seq_dir)
                else:
                    lmdb_env_seq = None

            for batch_idx, (inputs, seq_name, starting_frame) in enumerate(self.loader):

                prev_hidden_temporal_list = None
                max_ii = min(len(inputs), args.length_clip)

                if args.overlay_masks:
                    base_dir = results_dir + '/' + seq_name[0] + '/'
                    make_dir(base_dir)

                if args.dataset == 'youtube':

                    seq_data = data['videos'][seq_name[0]]['objects']
                    frame_names = []
                    frame_names_with_new_objects = []
                    instance_ids = []

                    for obj_id in seq_data.keys():
                        instance_ids.append(int(obj_id))
                        frame_names_with_new_objects.append(seq_data[obj_id]['frames'][0])
                        for frame_name in seq_data[obj_id]['frames']:
                            if frame_name not in frame_names:
                                frame_names.append(frame_name)

                    frame_names.sort()
                    frame_names_with_new_objects_idxs = []
                    for kk in range(len(frame_names_with_new_objects)):
                        new_frame_idx = frame_names.index(frame_names_with_new_objects[kk])
                        frame_names_with_new_objects_idxs.append(new_frame_idx)

                elif args.dataset == 'davis2017':

                    key_db = osp.basename(seq_name[0])

                    if not lmdb_env_seq == None:
                        with lmdb_env_seq.begin() as txn:
                            _files_vec = txn.get(key_db.encode()).decode().split('|')
                            _files = [osp.splitext(f)[0] for f in _files_vec]
                    else:
                        seq_dir = osp.join(cfg['PATH']['SEQUENCES'], key_db)
                        _files_vec = os.listdir(seq_dir)
                        _files = [osp.splitext(f)[0] for f in _files_vec]

                    frame_names = sorted(_files)
                else:

                    key_db = osp.basename(seq_name[0])

                    if not lmdb_env_seq == None:
                        with lmdb_env_seq.begin() as txn:
                            _files_vec = txn.get(key_db.encode()).decode().split('|')
                            _files = [osp.splitext(f)[0] for f in _files_vec]
                    else:
                        seq_dir = osp.join(cfg['PATH']['SEQUENCES'], key_db)
                        _files_vec = os.listdir(seq_dir)
                        _files = [osp.splitext(f)[0] for f in _files_vec]

                    # frame_names_with_new_objects_idxs = [3,6,9]
                    frame_names = sorted(_files)

                for ii in range(max_ii):

                    #                x: input images (N consecutive frames from M different sequences)
                    #                y_mask: ground truth annotations (some of them are zeros to have a fixed length in number of object instances)
                    #                sw_mask: this mask indicates which masks from y_mask are valid
                    x = batch_to_var_test(args, inputs[ii])

                    print(seq_name[0] + '/' + frame_names[ii])

                    if ii == 0:

                        frame_name = frame_names[0]
                        if args.dataset == 'youtube':
                            annotation = Image.open(
                                '../../databases/YouTubeVOS/val/Annotations/' + seq_name[0] + '/' + frame_name + '.png')
                            annot = imresize(annotation, (256, 448), interp='nearest')
                        elif args.dataset == 'davis2017':
                            annotation = Image.open(
                                '../../databases/DAVIS2017/Annotations/480p/' + seq_name[0] + '/' + frame_name + '.png')
                            instance_ids = sorted(np.unique(annotation))
                            instance_ids = instance_ids if instance_ids[0] else instance_ids[1:]
                            if len(instance_ids) > 0:
                                instance_ids = instance_ids[:-1] if instance_ids[-1] == 255 else instance_ids
                            annot = imresize(annotation, (240, 427), interp='nearest')
                        else:  # kittimots
                            annotation = Image.open(
                                '../../databases/KITTIMOTS/Annotations/' + seq_name[0] + '/' + frame_name + '.png')
                            instance_ids = sorted(np.unique(annotation))
                            instance_ids = instance_ids if instance_ids[0] else instance_ids[1:]
                            print("IDS instances: ", instance_ids)
                            if len(instance_ids) > 0:
                                instance_ids = instance_ids[:-1] if instance_ids[-1] == 255 else instance_ids
                            annot = imresize(annotation, (256, 448), interp='nearest')

                        annot = np.expand_dims(annot, axis=0)
                        annot = torch.from_numpy(annot)
                        annot = annot.float()
                        annot = annot.numpy().squeeze()
                        annot = annot_from_mask(annot, instance_ids)
                        prev_mask = annot
                        prev_mask = np.expand_dims(prev_mask, axis=0)
                        prev_mask = torch.from_numpy(prev_mask)
                        y_mask = Variable(prev_mask.float(), requires_grad=False)
                        prev_mask = y_mask.cuda()
                        del annot

                    if args.dataset == 'youtube':
                        if ii > 0 and ii in frame_names_with_new_objects_idxs:

                            frame_name = frame_names[ii]
                            annotation = Image.open(
                                '../../databases/YouTubeVOS/val/Annotations/' + seq_name[0] + '/' + frame_name + '.png')
                            annot = imresize(annotation, (256, 448), interp='nearest')
                            annot = np.expand_dims(annot, axis=0)
                            annot = torch.from_numpy(annot)
                            annot = annot.float()
                            annot = annot.numpy().squeeze()
                            new_instance_ids = np.unique(annot)[1:]
                            annot = annot_from_mask(annot, new_instance_ids)
                            annot = np.expand_dims(annot, axis=0)
                            annot = torch.from_numpy(annot)
                            annot = Variable(annot.float(), requires_grad=False)
                            annot = annot.cuda()
                            for kk in new_instance_ids:
                                prev_mask[:, int(kk - 1), :] = annot[:, int(kk - 1), :]
                            del annot

                    # from one frame to the following frame the prev_hidden_temporal_list is updated.
                    outs, hidden_temporal_list = test_prev_mask(args, self.encoder, self.decoder, x,
                                                                prev_hidden_temporal_list, prev_mask)

                    base_dir_masks_sep = masks_sep_dir + '/' + seq_name[0] + '/'
                    make_dir(base_dir_masks_sep)

                    x_tmp = x.data.cpu().numpy()
                    height = x_tmp.shape[-2]
                    width = x_tmp.shape[-1]

                    for t in range(len(instance_ids)):
                        mask_pred = (torch.squeeze(outs[0, t, :])).cpu().numpy()
                        mask_pred = np.reshape(mask_pred, (height, width))
                        indxs_instance = np.where(mask_pred > 0.5)
                        mask2assess = np.zeros((height, width))
                        mask2assess[indxs_instance] = 255
                        toimage(mask2assess, cmin=0, cmax=255).save(
                            base_dir_masks_sep + frame_names[ii] + '_instance_%02d.png' % (t))

                    if args.overlay_masks:

                        frame_img = x.data.cpu().numpy()[0, :, :, :].squeeze()
                        frame_img = np.transpose(frame_img, (1, 2, 0))
                        mean = np.array([0.485, 0.456, 0.406])
                        std = np.array([0.229, 0.224, 0.225])
                        frame_img = std * frame_img + mean
                        frame_img = np.clip(frame_img, 0, 1)
                        plt.figure();
                        plt.axis('off')
                        plt.figure();
                        plt.axis('off')
                        plt.imshow(frame_img)

                        for t in range(len(instance_ids)):

                            mask_pred = (torch.squeeze(outs[0, t, :])).cpu().numpy()
                            mask_pred = np.reshape(mask_pred, (height, width))
                            ax = plt.gca()
                            tmp_img = np.ones((mask_pred.shape[0], mask_pred.shape[1], 3))
                            color_mask = np.array(colors[t]) / 255.0
                            for i in range(3):
                                tmp_img[:, :, i] = color_mask[i]
                            ax.imshow(np.dstack((tmp_img, mask_pred * 0.7)))

                        figname = base_dir + frame_names[ii] + '.png'
                        plt.savefig(figname, bbox_inches='tight')
                        plt.close()

                    if self.video_mode:
                        if args.only_spatial == False:
                            prev_hidden_temporal_list = hidden_temporal_list
                        if ii > 0:
                            prev_mask = outs
                        del x, hidden_temporal_list, outs


def dict_from_annots(self, annot_seq_dir, starting_frame):
    ids = []
    length = 10
    # we check the id of the group of annotations of lenght length_clip
    for i in range(length):
        annot_name = starting_frame + i
        annot = np.array(Image.open(osp.join(annot_seq_dir, str('%06d.png' % annot_name))).convert('P'))
        annot_unique_ids = np.unique(annot)  # unique id of the instances of the annotations
        ids = np.append(ids, annot_unique_ids)
    unique_ids = np.unique(ids)  # we filter for unique ids

    # create the dictionary of real ids of the subsequence of size length_clip
    dict = {}
    for j in range(len(unique_ids)):
        if j == 0:
            continue
        else:
            dict.update({str(int(unique_ids[j])): j})

    return dict
def last_ocurrence(sequence, number):

    last_position = -1
    for n in range(len(sequence)):
        if sequence[n] == number:
            last_position = n

    return last_position


def annots_from_dict(self, annot_seq_dir, starting_frame):
    ids = []
    length = 10
    # we check the id of the group of annotations of lenght length_clip
    for i in range(length):
        annot_name = starting_frame + i
        annot = np.array(Image.open(osp.join(annot_seq_dir, str('%06d.png' % annot_name))).convert('P'))
        annot_unique_ids = np.unique(annot)  # unique id of the instances of the annotations
        ids = np.append(ids, annot_unique_ids)
    unique_ids = np.unique(ids)  # we filter for unique ids

    # create the dictionary of real ids of the subsequence of size length_clip
    dict = {}
    for j in range(len(unique_ids)):
        if j == 0:
            continue
        else:
            dict.update({str(int(unique_ids[j])): j})

    return dict


def annot_from_mask(annot, instance_ids):
    h = annot.shape[0]
    w = annot.shape[1]

    total_num_instances = len(instance_ids)
    max_instance_id = 0
    if total_num_instances > 0:
        max_instance_id = int(np.max(instance_ids))
    num_instances = max(args.maxseqlen, max_instance_id)

    gt_seg = np.zeros((num_instances, h * w))

    for i in range(total_num_instances):
        id_instance = instance_ids[i]
        aux_mask = np.zeros((h, w))
        aux_mask[annot == id_instance] = 1
        gt_seg[int(id_instance) - 1, :] = np.reshape(aux_mask, h * w)

    gt_seg = gt_seg[:][:args.maxseqlen]

    return gt_seg


if __name__ == "__main__":

    parser = get_parser()
    args = parser.parse_args()

    gpu_id = args.gpu_id
    if args.use_gpu:
        torch.cuda.set_device(device=gpu_id)
        torch.cuda.manual_seed(args.seed)
    else:
        torch.manual_seed(args.seed)

    if not args.log_term:
        print("Eval logs will be saved to:", os.path.join('../models', args.model_name, 'eval.log'))
        sys.stdout = open(os.path.join('../models', args.model_name, 'eval.log'), 'w')

    E = Evaluate(args)
    E.run_eval()