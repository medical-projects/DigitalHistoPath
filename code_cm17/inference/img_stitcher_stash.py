from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import os
import glob
import random
from crf import do_crf,post_process_crf

import imgaug
from imgaug import augmenters as iaa
from PIL import Image
from tqdm import tqdm
import matplotlib.pyplot as plt

import openslide
import numpy as np
import tensorflow as tf
from tensorflow.keras import backend as K
from tensorflow.keras.models import Model
from tensorflow.keras.layers import Input, BatchNormalization, Conv2D, MaxPooling2D, AveragePooling2D, ZeroPadding2D, concatenate, Concatenate, UpSampling2D, Activation
from tensorflow.keras.losses import categorical_crossentropy
from tensorflow.keras.applications.densenet import DenseNet121
from tensorflow.keras.optimizers import Adam
from tensorflow.keras.callbacks import ModelCheckpoint, LearningRateScheduler, TensorBoard
from tensorflow.keras import metrics

from torch.utils.data import DataLoader, Dataset
from torchvision import transforms  # noqa

import sklearn.metrics
import io
import itertools
from six.moves import range

import time
import cv2
from skimage.color import rgb2hsv
from skimage.filters import threshold_otsu

import sys
sys.path.append(os.path.dirname(os.path.abspath(os.getcwd())))
from models.seg_models import get_inception_resnet_v2_unet_softmax, unet_densenet121
from models.deeplabv3p_original import Deeplabv3
# Random Seeds
np.random.seed(0)
random.seed(0)
tf.set_random_seed(0)
import gc
import pandas as pd

import tifffile 
import skimage.io as io


# In[50]:


# Image Helper Functions
def imsave(*args, **kwargs):
     """
     Concatenate the images given in args and saves them as a single image in the specified output destination.
     Images should be numpy arrays and have same dimensions along the 0 axis.
     imsave(im1,im2,out="sample.png")
     """
     args_list = list(args)
     for i in range(len(args_list)):
         if type(args_list[i]) != np.ndarray:
             print("Not a numpy array")
             return 0
         if len(args_list[i].shape) == 2:
             args_list[i] = np.dstack([args_list[i]]*3)
             if args_list[i].max() == 1:
                args_list[i] = args_list[i]*255

     out_destination = kwargs.get("out",'')
     try:
         concatenated_arr = np.concatenate(args_list,axis=1)
         im = Image.fromarray(np.uint8(concatenated_arr))
     except Exception as e:
         print(e)
         import ipdb; ipdb.set_trace()
         return 0
     if out_destination:
         print("Saving to %s"%(out_destination))
         im.save(out_destination)
     else:
        return im

def imshow(*args,**kwargs):
    """ Handy function to show multiple plots in on row, possibly with different cmaps and titles
    Usage:
    imshow(img1, title="myPlot")
    imshow(img1,img2, title=['title1','title2'])
    imshow(img1,img2, cmap='hot')
    imshow(img1,img2,cmap=['gray','Blues']) """
    cmap = kwargs.get('cmap', 'gray')
    title= kwargs.get('title','')
    axis_off = kwargs.get('axis_off','')
    if len(args)==0:
        raise ValueError("No images given to imshow")
    elif len(args)==1:
        plt.title(title)
        plt.imshow(args[0], interpolation='none')
    else:
        n=len(args)
        if type(cmap)==str:
            cmap = [cmap]*n
        if type(title)==str:
            title= [title]*n
        plt.figure(figsize=(n*5,10))
        for i in range(n):
            plt.subplot(1,n,i+1)
            plt.title(title[i])
            plt.imshow(args[i], cmap[i])
            if axis_off: 
              plt.axis('off')  
    plt.show()
def normalize_minmax(data):
    """
    Normalize contrast across volume
    """
    _min = np.float(np.min(data))
    _max = np.float(np.max(data))
    if (_max-_min)!=0:
        img = (data - _min) / (_max-_min)
    else:
        img = np.zeros_like(data)            
    return img

# Functions
def BinMorphoProcessMask(mask):
    """
    Binary operation performed on tissue mask
    """
    close_kernel = np.ones((20, 20), dtype=np.uint8)
    image_close = cv2.morphologyEx(np.array(mask), cv2.MORPH_CLOSE, close_kernel)
    open_kernel = np.ones((5, 5), dtype=np.uint8)
    image_open = cv2.morphologyEx(np.array(image_close), cv2.MORPH_OPEN, open_kernel)
    kernel = np.ones((20, 20), dtype=np.uint8)
    image = cv2.dilate(image_open,kernel,iterations = 1)
    return image

def get_bbox(cont_img, rgb_image=None):
    temp_img = np.uint8(cont_img.copy())
    _,contours, _ = cv2.findContours(temp_img, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    rgb_contour = None
    if rgb_image is not None:
        rgb_contour = rgb_image.copy()
        line_color = (0, 0, 255)  # blue color code
        cv2.drawContours(rgb_contour, contours, -1, line_color, 2)
    bounding_boxes = [cv2.boundingRect(c) for c in contours]
    for x, y, h, w in bounding_boxes:
        rgb_contour = cv2.rectangle(rgb_contour,(x,y),(x+h,y+w),(0,255,0),2)
    return bounding_boxes, rgb_contour

def get_all_bbox_masks(mask, stride_factor):
    """
    Find the bbox and corresponding masks
    """
    bbox_mask = np.zeros_like(mask)
    bounding_boxes, _ = get_bbox(mask)
    y_size, x_size = bbox_mask.shape
    for x, y, h, w in bounding_boxes:
        x_min = x - stride_factor
        x_max = x + h + stride_factor
        y_min = y - stride_factor
        y_max = y + w + stride_factor
        if x_min < 0: 
         x_min = 0
        if y_min < 0: 
         y_min = 0
        if x_max > x_size: 
         x_max = x_size - 1
        if y_max > y_size: 
         y_max = y_size - 1      
        bbox_mask[y_min:y_max, x_min:x_max]=1
    return bbox_mask

def get_all_bbox_masks_with_stride(mask, stride_factor):
    """
    Find the bbox and corresponding masks
    """
    bbox_mask = np.zeros_like(mask)
    bounding_boxes, _ = get_bbox(mask)
    y_size, x_size = bbox_mask.shape
    for x, y, h, w in bounding_boxes:
        x_min = x - stride_factor
        x_max = x + h + stride_factor
        y_min = y - stride_factor
        y_max = y + w + stride_factor
        if x_min < 0: 
         x_min = 0
        if y_min < 0: 
         y_min = 0
        if x_max > x_size: 
         x_max = x_size - 1
        if y_max > y_size: 
         y_max = y_size - 1      
        bbox_mask[y_min:y_max:stride_factor, x_min:x_max:stride_factor]=1
        
    return bbox_mask

def find_largest_bbox(mask, stride_factor):
    """
    Find the largest bounding box encompassing all the blobs
    """
    y_size, x_size = mask.shape
    x, y = np.where(mask==1)
    bbox_mask = np.zeros_like(mask)
    x_min = np.min(x) - stride_factor
    x_max = np.max(x) + stride_factor
    y_min = np.min(y) - stride_factor
    y_max = np.max(y) + stride_factor
    
    if x_min < 0: 
     x_min = 0
    
    if y_min < 0: 
     y_min = 0

    if x_max > x_size: 
     x_max = x_size - 1
    
    if y_min > y_size: 
     y_max = y_size - 1    
    
    bbox_mask[x_min:x_max, y_min:y_max]=1
    return bbox_mask
    
    
def TissueMaskGeneration(slide_obj, level, RGB_min=50):
    img_RGB = slide_obj.read_region((0, 0),level,slide_obj.level_dimensions[level])
    img_RGB = np.transpose(np.array(img_RGB.convert('RGB')),axes=[1,0,2])
    img_HSV = rgb2hsv(img_RGB)
    background_R = img_RGB[:, :, 0] > threshold_otsu(img_RGB[:, :, 0])
    background_G = img_RGB[:, :, 1] > threshold_otsu(img_RGB[:, :, 1])
    background_B = img_RGB[:, :, 2] > threshold_otsu(img_RGB[:, :, 2])
    tissue_RGB = np.logical_not(background_R & background_G & background_B)
    tissue_S = img_HSV[:, :, 1] > threshold_otsu(img_HSV[:, :, 1])
    min_R = img_RGB[:, :, 0] > RGB_min
    min_G = img_RGB[:, :, 1] > RGB_min
    min_B = img_RGB[:, :, 2] > RGB_min

    tissue_mask = tissue_S & tissue_RGB & min_R & min_G & min_B
    # r = img_RGB[:,:,0] < 235
    # g = img_RGB[:,:,1] < 210
    # b = img_RGB[:,:,2] < 235
    # tissue_mask = np.logical_or(r,np.logical_or(g,b))
    return tissue_mask 
def TissueMaskGenerationPatch(patchRGB):
    '''
    Returns mask of tissue that obeys the threshold set by paip
    '''
    r = patchRGB[:,:,0] < 235
    g = patchRGB[:,:,1] < 210
    b = patchRGB[:,:,2] < 235
    tissue_mask = np.logical_or(r,np.logical_or(g,b))
    return tissue_mask 
    
def TissueMaskGeneration_BIN(slide_obj, level):
    img_RGB = np.transpose(np.array(slide_obj.read_region((0, 0),
                       level,
                       slide_obj.level_dimensions[level]).convert('RGB')),
                       axes=[1, 0, 2])    
    img_HSV = cv2.cvtColor(img_RGB, cv2.COLOR_BGR2HSV)
    img_S = img_HSV[:, :, 1]
    _,tissue_mask = cv2.threshold(img_S, 0, 255, cv2.THRESH_BINARY)
    return np.array(tissue_mask)

def TissueMaskGeneration_BIN_OTSU(slide_obj, level):
    img_RGB = np.transpose(np.array(slide_obj.read_region((0, 0),
                       level,
                       slide_obj.level_dimensions[level]).convert('RGB')),
                       axes=[1, 0, 2])    
    img_HSV = cv2.cvtColor(img_RGB, cv2.COLOR_BGR2HSV)
    img_S = img_HSV[:, :, 1]
    _,tissue_mask = cv2.threshold(img_S, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    return np.array(tissue_mask)

def labelthreshold(image, threshold=0.5):
    np.place(image,image>=threshold, 1)
    np.place(image,image<threshold, 0)
    return np.uint8(image)

def calc_jacc_score(x,y,smoothing=1):
    for var in [x,y]:
        np.place(var,var==255,1)
    
    numerator = np.sum(x*y)
    denominator = np.sum(np.logical_or(x,y))
    return (numerator+smoothing)/(denominator+smoothing)



# In[41]:


# DataLoader Implementation
class WSIStridedPatchDataset(Dataset):
    """
    Data producer that generate all the square grids, e.g. 3x3, of patches,
    from a WSI and its tissue mask, and their corresponding indices with
    respect to the tissue mask
    """
    def __init__(self, wsi_path, mask_path, label_path=None, image_size=256,
                 normalize=True, flip='NONE', rotate='NONE',                
                 level=5, sampling_stride=16, roi_masking=True):
        """
        Initialize the data producer.

        Arguments:
            wsi_path: string, path to WSI file
            mask_path: string, path to mask file in numpy format OR None
            label_mask_path: string, path to ground-truth label mask path in tif file or
                            None (incase of Normal WSI or test-time)
            image_size: int, size of the image before splitting into grid, e.g. 768
            patch_size: int, size of the patch, e.g. 256
            crop_size: int, size of the final crop that is feed into a CNN,
                e.g. 224 for ResNet
            normalize: bool, if normalize the [0, 255] pixel values to [-1, 1],
                mostly False for debuging purpose
            flip: string, 'NONE' or 'FLIP_LEFT_RIGHT' indicating the flip type
            rotate: string, 'NONE' or 'ROTATE_90' or 'ROTATE_180' or
                'ROTATE_270', indicating the rotate type
            level: Level to extract the WSI tissue mask
            roi_masking: True: Multiplies the strided WSI with tissue mask to eliminate white spaces,
                                False: Ensures inference is done on the entire WSI   
            sampling_stride: Number of pixels to skip in the tissue mask, basically it's the overlap
                            fraction when patches are extracted from WSI during inference.
                            stride=1 -> consecutive pixels are utilized
                            stride= image_size/pow(2, level) -> non-overalaping patches 
        """
        self._wsi_path = wsi_path
        self._mask_path = mask_path
        self._label_path = label_path
        self._image_size = image_size
        self._normalize = normalize
        self._flip = flip
        self._rotate = rotate
        self._level = level
        self._sampling_stride = sampling_stride
        self._roi_masking = roi_masking
        
        self._preprocess()

    def _preprocess(self):
        self._slide = openslide.OpenSlide(self._wsi_path)
        
        if self._label_path is not None:
            self._label_slide = openslide.OpenSlide(self._label_path)
        
        X_slide, Y_slide = self._slide.level_dimensions[0]
        print("Image dimensions: (%d,%d)" %(X_slide,Y_slide))
        
        factor = self._sampling_stride

        
        if self._mask_path is not None:
            mask_file_name = os.path.basename(self._mask_path)
            if mask_file_name.endswith('.tiff'):
                mask_obj = openslide.OpenSlide(self._mask_path)
                self._mask = np.array(mask_obj.read_region((0, 0),
                       self._level,
                       mask_obj.level_dimensions[self._level]).convert('L')).T
                np.place(self._mask,self._mask>0,255)
        else:
            # Generate tissue mask on the fly    
            
            self._mask = TissueMaskGeneration(self._slide, self._level)
        # morphological operations ensure the holes are filled in tissue mask
        # and minor points are aggregated to form a larger chunk         

        self._mask = BinMorphoProcessMask(np.uint8(self._mask))
        # self._all_bbox_mask = get_all_bbox_masks(self._mask, factor)
        # self._largest_bbox_mask = find_largest_bbox(self._mask, factor)
        # self._all_strided_bbox_mask = get_all_bbox_masks_with_stride(self._mask, factor)

        X_mask, Y_mask = self._mask.shape
        # print (self._mask.shape, np.where(self._mask>0))
        # imshow(self._mask.T)
        # cm17 dataset had issues with images being power's of 2 precisely        
#         if X_slide != X_mask or Y_slide != Y_mask:
        print('Mask (%d,%d) and Slide(%d,%d) '%(X_mask,Y_mask,X_slide,Y_slide))
        if X_slide // X_mask != Y_slide // Y_mask:
            raise Exception('Slide/Mask dimension does not match ,'
                            ' X_slide / X_mask : {} / {},'
                            ' Y_slide / Y_mask : {} / {}'
                            .format(X_slide, X_mask, Y_slide, Y_mask))

        self._resolution = np.round(X_slide * 1.0 / X_mask)
        if not np.log2(self._resolution).is_integer():
            raise Exception('Resolution (X_slide / X_mask) is not power of 2 :'
                            ' {}'.format(self._resolution))
             
        # all the idces for tissue region from the tissue mask  
        self._strided_mask =  np.ones_like(self._mask)
        ones_mask = np.zeros_like(self._mask)
        ones_mask[::factor, ::factor] = self._strided_mask[::factor, ::factor]
        
        
        if self._roi_masking:
            self._strided_mask = ones_mask*self._mask   
            # self._strided_mask = ones_mask*self._largest_bbox_mask   
            # self._strided_mask = ones_mask*self._all_bbox_mask 
            # self._strided_mask = self._all_strided_bbox_mask  
        else:
            self._strided_mask = ones_mask  
        # print (np.count_nonzero(self._strided_mask), np.count_nonzero(self._mask[::factor, ::factor]))
        # imshow(self._strided_mask.T, self._mask[::factor, ::factor].T)
        # imshow(self._mask.T, self._strided_mask.T)
 
        self._X_idcs, self._Y_idcs = np.where(self._strided_mask)        
        self._idcs_num = len(self._X_idcs)

    def __len__(self):        
        return self._idcs_num 

    def save_scaled_imgs(self):
        scld_dms = self._slide.level_dimensions[2]
        self._slide_scaled = self._slide.read_region((0,0),2,scld_dms)
        
        if self._label_path is not None:
            self._label_scaled = np.array(self._label_slide.read_region((0,0),4,scld_dms).convert('L'))
            np.place(self._label_scaled,self._label_scaled>0,255)
        
    def save_get_mask(self, save_path):
        np.save(save_path, self._mask)

    def get_mask(self):
        return self._mask

    def get_strided_mask(self):
        return self._strided_mask
    
    def __getitem__(self, idx):
        x_coord, y_coord = self._X_idcs[idx], self._Y_idcs[idx]
        
        x_max_dim,y_max_dim = self._slide.level_dimensions[0]

        # x = int(x_coord * self._resolution)
        # y = int(y_coord * self._resolution)    

        x = int(x_coord * self._resolution - self._image_size//2)
        y = int(y_coord * self._resolution - self._image_size//2)    
#         x = int(x_coord * self._resolution)
#         y = int(y_coord * self._resolution)    
        
        #If Image goes out of bounds
        if x>(x_max_dim - image_size):
            x = x_max_dim - image_size
        elif x<0:
            x = 0
        if y>(y_max_dim - image_size):
            y = y_max_dim - image_size
        elif y<0:
            y = 0
    
        #Converting pil image to np array transposes the w and h
        img = np.transpose(self._slide.read_region(
            (x, y), 0, (self._image_size, self._image_size)).convert('RGB'),[1,0,2])
        
        if self._label_path is not None:
            label_img = self._label_slide.read_region(
                (x, y), 0, (self._image_size, self._image_size)).convert('L')
        else:
            #print('No label img')
            label_img = Image.fromarray(np.zeros((self._image_size, self._image_size), dtype=np.uint8))
        
        if self._flip == 'FLIP_LEFT_RIGHT':
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
            label_img = label_img.transpose(Image.FLIP_LEFT_RIGHT)
            
        if self._rotate == 'ROTATE_90':
            img = img.transpose(Image.ROTATE_90)
            label_img = label_img.transpose(Image.ROTATE_90)
            
        if self._rotate == 'ROTATE_180':
            img = img.transpose(Image.ROTATE_180)
            label_img = label_img.transpose(Image.ROTATE_180)

        if self._rotate == 'ROTATE_270':
            img = img.transpose(Image.ROTATE_270)
            label_img = label_img.transpose(Image.ROTATE_270)

        # PIL image:   H x W x C
        img = np.array(img, dtype=np.float32)
        label_img = np.array(label_img, dtype=np.uint8)
        np.place(label_img, label_img>0, 255)

        if self._normalize:
            img = (img - 128.0)/128.0
   
        return (img, x, y, label_img)
#CONFIG
# batch_size = 40
# image_size = 256
# sampling_stride = 128
batch_size = 100
image_size = 256
sampling_stride = 256
kfold_k = 5
fold = 0
out_dir_root = '../../results/saved_imgs'

#Model loading
os.environ["CUDA_VISIBLE_DEVICES"] = '0'
core_config = tf.ConfigProto()
core_config.gpu_options.allow_growth = False
# core_config.gpu_options.per_process_gpu_memory_fraction=0.47
session =tf.Session(config=core_config) 
K.set_session(session)

#Inception
def load_incep_resnet(model_path):
    model = get_inception_resnet_v2_unet_softmax((None, None), weights=None)
    model.load_weights(model_path)
    print ("Loaded Model Weights %s" % model_path)
    return model

def load_unet_densenet(model_path):
    model = unet_densenet121((None, None), weights=None)
    model.load_weights(model_path)
    print ("Loaded Model Weights %s" % model_path)
    return model

def load_deeplabv3(model_path, OS):
    model = Deeplabv3(input_shape=(image_size, image_size, 3),weights=None,classes=2,activation='softmax',backbone='xception',OS=OS)
    model.load_weights(model_path)
    print ("Loaded Model Weights %s" % model_path)
    return model

# model_path = glob.glob('../../results/saved_models/incep_viable_200k/5fold_0/sel-model.10*')[0]
model_dict = {
        'train_30': load_incep_resnet('../../results/saved_models/incep_imagenet_200k/5fold_0/sel-model.11-0.16.h5'),
        'train_31': load_incep_resnet('../../results/saved_models/incep_imagenet_200k/5fold_0/sel-model.12-0.17.h5'),
        # 'train_23': load_unet_densenet('../../results/saved_models/dense_viable_200k/5fold_3/model.28-0.08.h5'),
        # 'train_24': load_deeplabv3('../../results/saved_models/deeplab_viable_200k/5fold_4/sel-model.20-0.13.h5',OS=16),
        }
model_keys = list(model_dict.keys())
ensemble_key = 'train_32'
model_dict[ensemble_key] = 'ensemble'

out_dir_dict = {}
for key in model_dict.keys():
    out_dir_dict[key] = os.path.join(out_dir_root,key)
    try:
        os.makedirs(out_dir_dict[key])
    except FileExistsError:
        if os.listdir(out_dir_dict[key]) != []:
            print("Out folder exists and is non-empty, continue?")
            print(out_dir_dict[key])
            input()

#Stitcher
start_time = time.time()

mode = 'validation'
if fold == 'all':
    sample_ids = os.listdir('../../data/raw-data/train')
    sample_ids.sort()
else:
    sample_ids = [ x.split('/')[-2] for x in list(pd.read_csv('../../data/raw-data/cross_val_splits_%d_whole/%s_fold_%d.csv'%(kfold_k,mode,fold))['Image_Path'])]

print(sample_ids)

print("Total %d" %len(sample_ids))
# sample_ids = sample_ids[1:]
# wsi_paths = glob.glob('../../data/raw-data/valid/*svs')
# print("Total %d" %len(wsi_paths))
# for i,wsi_path in enumerate(wsi_paths[1:2]):

close_kernel = np.ones((20, 20), dtype=np.uint8)
total_jacc_score_dict = {}
for key in model_dict.keys():
    total_jacc_score_dict[key] = 0
for i,sample_id in enumerate(sample_ids):
    sample_dir = os.path.join('..','..','data','raw-data','train',sample_id)
    wsi_path = glob.glob(os.path.join(sample_dir,'*.svs'))[0]
    label_path = glob.glob(os.path.join(sample_dir,'*viable*.tiff'))[0]

    print(i+1,'/', len(sample_ids),sample_id)
    wsi_obj = openslide.OpenSlide(wsi_path)
    x_max_dim,y_max_dim = wsi_obj.level_dimensions[0]
    scld_dms = wsi_obj.level_dimensions[2]
    count_map = np.zeros(wsi_obj.level_dimensions[0],dtype='uint8')
    prd_im_fll_dict = {}
    for key in model_dict.keys():
        # prd_im_fll_dict[key] = np.zeros(wsi_obj.level_dimensions[0])
        prd_im_fll_dict[key] = np.memmap('/home/brats/Documents/memmaps/%s.dat'%(key), dtype=np.float32,mode='w+', shape=(wsi_obj.level_dimensions[0]))                                                   
    
    scale = lambda x: cv2.resize(x,tuple(reversed(scld_dms))).T
    mask_path = None
    start_time = time.time()
    dataset_obj = WSIStridedPatchDataset(wsi_path, 
                                        mask_path,
                                        label_path,
                                        image_size=image_size,
                                        normalize=True,
                                        flip=None, rotate=None,
                                        level=2, sampling_stride=sampling_stride//16, roi_masking=True)

    dataloader = DataLoader(dataset_obj, batch_size=batch_size, num_workers=8, drop_last=True)
    dataset_obj.save_scaled_imgs()
    # out_file = wsi_path.split('/')[-1].split('.')[0]
    out_file = sample_id

    print(dataset_obj.get_mask().shape)
    st_im = dataset_obj.get_strided_mask()
    mask_im = np.dstack([dataset_obj.get_mask().T]*3).astype('uint8')*255
    st_im = np.dstack([dataset_obj.get_strided_mask().T]*3).astype('uint8')*255
    im_im = np.array(dataset_obj._slide_scaled.convert('RGB'))
    ov_im = mask_im/2 + im_im/2
    for key in model_dict.keys():
        imsave(ov_im.astype('uint8'),mask_im,st_im,(im_im), out=os.path.join(out_dir_dict[key],'mask_'+out_file+'.png'))

    print("Total iterations: %d %d" % (dataloader.__len__(), dataloader.dataset.__len__()))
    for i,(data, xes, ys, label) in enumerate(dataloader):
        tmp_pls= lambda x: x + image_size
        tmp_mns= lambda x: x 
        image_patches = data.cpu().data.numpy()
        image_patches = data.cpu().data.numpy()
        
        pred_map_dict = {}
        pred_map_dict[ensemble_key] = 0
        for key in model_keys:
            pred_map_dict[key] = model_dict[key].predict(image_patches,verbose=0,batch_size=2)
            pred_map_dict[ensemble_key]+=pred_map_dict[key]
        pred_map_dict[ensemble_key]/=len(model_keys)

        actual_batch_size =  image_patches.shape[0]
        for j in range(actual_batch_size):
            x = int(xes[j])
            y = int(ys[j])

            wsi_img = image_patches[j]*128+128
            patch_mask = TissueMaskGenerationPatch(wsi_img)


            #CRF
            # prediction = red_map[j,:,:,:]
            # prediction = post_process_crf(wsi_img,prediction,2)
            for key in model_dict.keys():
                prediction = pred_map_dict[key][j,:,:,1]

                # prediction = cv2.morphologyEx(np.array(prediction), cv2.MORPH_CLOSE, close_kernel)
                prediction*=patch_mask

                prd_im_fll_dict[key][tmp_mns(x):tmp_pls(x),tmp_mns(y):tmp_pls(y)] += prediction
            count_map[tmp_mns(x):tmp_pls(x),tmp_mns(y):tmp_pls(y)] += np.ones((image_size,image_size),dtype='uint8')
                                                                         
        if (i+1)%100==0 or i<10:
            print("Completed %i Time elapsed %.2f min | Max count %d "%(i,(time.time()-start_time)/60,count_map.max()))
        
    print("Fully completed %i Time elapsed %.2f min | Max count %d "%(i,(time.time()-start_time)/60,count_map.max()))
    start_time = time.time()

    print("\t Dividing by count_map")
    np.place(count_map, count_map==0, 1)
    for key in model_dict.keys():
        prd_im_fll_dict[key]/=count_map
    # scaled_count_map = scale(count_map)
    # scaled_count_map = scaled_count_map*255//scaled_count_map.max()
    del count_map
    gc.collect()

    print("\t Scaling prediciton")
    prob_map_dict = {}
    for key in  model_dict.keys():
        prob_map_dict[key] = scale(prd_im_fll_dict[key])
        prob_map_dict[key] = (prob_map_dict[key]*255).astype('uint8')
    # prd_im_fll = labelthreshold(prd_im_fll,0.8).astype(np.uint8)
    print("\t Thresholding prediction")
    threshold = 0.5
    for key in  model_dict.keys():
        np.place(prd_im_fll_dict[key],prd_im_fll_dict[key]>=threshold, 1)
        np.place(prd_im_fll_dict[key],prd_im_fll_dict[key]<threshold, 0)

    #Calc jaccard score
    start_time = time.time()
    print("\t Calculating jacc score")
    ground_truth = io.imread(glob.glob(os.path.join(sample_dir,'*viable*.tif'))[0]).astype('uint8').T
    for key in  model_dict.keys():
        jacc_score= calc_jacc_score(prd_im_fll_dict[key],ground_truth)
        gc.collect()
        print("\t %s Jacc score: %f" % (key,jacc_score))
        with open(os.path.join(out_dir_dict[key],'jacc_scores.txt'), 'a') as f:
            f.write("%s,%f\n" %(sample_id,jacc_score))
            total_jacc_score_dict[key]+=jacc_score
    del ground_truth
    gc.collect()
    print("\t Calculated in %f" % ((time.time() - start_time)/60))
    # print("Saving to %s" %(out_file))
    # tifffile.imsave(os.path.join(out_dir,out_file)+'.tif', prd_im_fll.T, compress=9)

    scaled_prd_im_fll_dict = {}
    for key in  model_dict.keys():
        scaled_prd_im_fll_dict[key] = scale(prd_im_fll_dict[key])
    del prd_im_fll_dict
    gc.collect()

    # mask_im = np.dstack([dataset_obj.get_mask().T]*3).astype('uint8')*255
    mask_im = np.dstack([TissueMaskGenerationPatch(im_im)]*3).astype('uint8')*255
    for key in  model_dict.keys():
        mask_im[:,:,0] = scaled_prd_im_fll_dict[key]*255
        imsave(mask_im,prob_map_dict[key],scaled_prd_im_fll_dict[key],dataset_obj._label_scaled,im_im,out=os.path.join(out_dir_dict[key],'ref_'+out_file)+'.png')

for key in  model_dict.keys():
    with open(os.path.join(out_dir_dict[key],'jacc_scores.txt'), 'a') as f:
        f.write("Total,%f\n" %(total_jacc_score_dict[key]/len(sample_ids)))