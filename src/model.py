import os
import numpy as np
import tensorflow as tf
from sklearn.utils import shuffle
import glob
import net.utility.draw  as nud
import mv3d_net
import net.blocks as blocks
import data
import net.processing.boxes3d  as box
from net.rpn_target_op import make_bases, make_anchors, rpn_target
from net.rcnn_target_op import rcnn_target
from net.rpn_nms_op     import draw_rpn_proposal
from net.rcnn_nms_op    import rcnn_nms, draw_rcnn_nms, draw_rcnn,draw_rcnn_nms_with_gt
from net.rpn_target_op  import draw_rpn_gt, draw_rpn_targets, draw_rpn_labels
from net.rcnn_target_op import draw_rcnn_targets, draw_rcnn_labels
import net.utility.file as utilfile
from config import cfg
import config
from net.processing.boxes import non_max_suppress
import utils.batch_loading as dataset
from utils.timer import timer


#http://3dimage.ee.tsinghua.edu.cn/cxz
# "Multi-View 3D Object Detection Network for Autonomous Driving" - Xiaozhi Chen, CVPR 2017

def get_top_feature_shape(top_shape, stride):
    return (top_shape[0]//stride, top_shape[1]//stride)

def project_to_roi3d(top_rois):
    num = len(top_rois)
    # rois3d = np.zeros((num,8,3))
    rois3d = box.top_box_to_box3d(top_rois[:, 1:5])
    return rois3d


def project_to_rgb_roi(rois3d):
    num  = len(rois3d)
    rois = np.zeros((num,5),dtype=np.int32)
    projections = box.box3d_to_rgb_projections(rois3d)
    for n in range(num):
        qs = projections[n]
        minx = np.min(qs[:,0])
        maxx = np.max(qs[:,0])
        miny = np.min(qs[:,1])
        maxy = np.max(qs[:,1])
        rois[n,1:5] = minx,miny,maxx,maxy

    return rois


# todo: finished project_to_front_roi
def  project_to_front_roi(rois3d):
    num  = len(rois3d)
    rois = np.zeros((num,5),dtype=np.int32)

    return rois

class MV3D(object):

    def __init__(self):

        self.stride=8
        self.num_class = 2  # incude background

        ratios=np.array([0.5,1,2], dtype=np.float32)
        scales=np.array([1,2,3],   dtype=np.float32)


        self.bases = make_bases(
            base_size = 16,
            ratios=ratios,  #aspet ratio
            scales=scales
        )

        # output dir, etc
        utilfile.makedirs(cfg.CHECKPOINT_DIR)
        utilfile.makedirs(os.path.join(cfg.CHECKPOINT_DIR,'reg'))
        self.log = utilfile.Logger(cfg.LOG_DIR+'/log.txt', mode='a')

        self.validation_set=None
        self.log_num=0
        self.log_max = 10

    def proposal(self):
        pass

    def log_rpn(self, subdir, top_image, batch_gt_top_boxes, batch_gt_labels, batch_top_inds, batch_top_labels,
                batch_top_pos_inds, batch_top_targets, batch_proposals, batch_proposal_scores):

        img_gt = draw_rpn_gt(top_image, batch_gt_top_boxes, batch_gt_labels)
        img_label = draw_rpn_labels(top_image, self.top_view_anchors, batch_top_inds, batch_top_labels)
        img_target = draw_rpn_targets(top_image, self.top_view_anchors, batch_top_pos_inds, batch_top_targets)
        nud.imsave('img_rpn_gt', img_gt,subdir)
        nud.imsave('img_rpn_label', img_label,subdir)
        nud.imsave('img_rpn_target', img_target,subdir)

        rpn_proposal = draw_rpn_proposal(top_image, batch_proposals, batch_proposal_scores, draw_num=20)
        nud.imsave(os.path.join(subdir, 'img_rpn_proposal'), rpn_proposal)

    def log_fusion_net(self, subdir, top_image, batch_top_rois, batch_fuse_labels,
                       batch_fuse_targets, rgb, batch_rgb_rois):
        img_label = draw_rcnn_labels(top_image, batch_top_rois, batch_fuse_labels)
        img_target = draw_rcnn_targets(top_image, batch_top_rois, batch_fuse_labels, batch_fuse_targets)
        nud.imsave('img_rcnn_label', img_label,subdir)
        nud.imsave('img_rcnn_target', img_target,subdir)

        img_rgb_rois = box.draw_boxes(rgb, batch_rgb_rois[:, 1:5], color=(255, 0, 255), thickness=1)
        nud.imsave('img_rgb_rois', img_rgb_rois, subdir)

    def log_prediction(self, subdir, batch_proposals, batch_proposal_scores, fd1, batch_top_view, batch_front_view,
                       batch_rgb_images, rgb, batch_gt_boxes3d, top_image):

        net=self.training_net
        sess=self.training_sess

        batch_top_rois_2 = batch_proposals[batch_proposal_scores > 0.1, :]
        batch_rois3d_2 = project_to_roi3d(batch_top_rois_2)
        batch_front_rois_2 = project_to_front_roi(batch_rois3d_2)
        batch_rgb_rois_2 = project_to_rgb_roi(batch_rois3d_2)

        fd2_2 = {
            **fd1,

            net['top_view']: batch_top_view,
            net['front_view']: batch_front_view,
            net['rgb_images']: batch_rgb_images,

            net['top_rois']: batch_top_rois_2,
            net['front_rois']: batch_front_rois_2,
            net['rgb_rois']: batch_rgb_rois_2,

        }

        batch_fuse_probs_2, batch_fuse_deltas_2 = \
            sess.run([net['fuse_probs'], net['fuse_deltas']], fd2_2)
        probs_2, boxes3d_2 = rcnn_nms(batch_fuse_probs_2, batch_fuse_deltas_2, batch_rois3d_2,
                                      score_threshold=0.5)

        predict_rgb_view = draw_rcnn_nms_with_gt(rgb, boxes3d_2, batch_gt_boxes3d)
        predict_top_view = box.draw_box3d_on_top(top_image, boxes3d_2, color=(80, 0, 0))
        nud.imsave('predict_rgb_view' , predict_rgb_view, subdir)
        nud.imsave( 'predict_top_view' , predict_top_view,subdir)

    def log_info(self, subdir, info):
        dir = os.path.join(cfg.LOG_DIR, subdir)
        os.makedirs(dir, exist_ok=True)
        with open(os.path.join(dir,'info.txt'), 'w') as info_file:
            info_file.write(info)

    def batch_data_is_invalid(self,train_gt_boxes3d):
        # todo : support batch size >1

        for i in range(len(train_gt_boxes3d)):
            if box.box3d_in_top_view(train_gt_boxes3d[i]):
                continue
            else:
                return True
        return False

    def validation_accuracy(self):
        net=self.training_net
        sess=self.training_sess
        batch_size=1

        top_cls_loss=net['top_cls_loss']
        top_reg_loss=net['top_reg_loss']
        fuse_cls_loss=net['fuse_cls_loss']
        fuse_reg_loss=net['fuse_reg_loss']

        loss_sum=np.zeros(4)

        pass_step=0

        batch_rgb_images, batch_top_view, batch_front_view,\
        train_gt_labels, train_gt_boxes3d ,frame_id= self.validation_set.load(batch_size,shuffled=True)
        for i in range(30):
            if self.batch_data_is_invalid(train_gt_boxes3d[0]):
                batch_rgb_images, batch_top_view, batch_front_view, \
                train_gt_labels, train_gt_boxes3d, frame_id = self.validation_set.load(batch_size, shuffled=True)
                continue
            else:
                break

        batch_gt_labels = train_gt_labels[0]
        batch_gt_boxes3d = train_gt_boxes3d[0]
        batch_gt_top_boxes = data.box3d_to_top_box(batch_gt_boxes3d)


        ## run propsal generation
        fd1={
            net['top_view']: batch_top_view,
            net['top_anchors']:self.top_view_anchors,
            net['top_inside_inds']: self.inside_inds,

            blocks.IS_TRAIN_PHASE:  True
        }
        batch_proposals, batch_proposal_scores, batch_top_features = \
            sess.run([net['proposals'], net['proposal_scores'], net['top_features']],fd1)

        ## generate  train rois  for RPN
        batch_top_inds, batch_top_pos_inds, batch_top_labels, batch_top_targets  = \
            rpn_target ( self.top_view_anchors, self.inside_inds, batch_gt_labels,  batch_gt_top_boxes)

        batch_top_rois, batch_fuse_labels, batch_fuse_targets  = \
             rcnn_target(  batch_proposals, batch_gt_labels, batch_gt_top_boxes, batch_gt_boxes3d )

        batch_rois3d	 = project_to_roi3d    (batch_top_rois)
        batch_front_rois = project_to_front_roi(batch_rois3d  )
        batch_rgb_rois   = project_to_rgb_roi  (batch_rois3d  )


        ## run classification and regression loss -----------
        fd2={
            **fd1,

            net['top_view']: batch_top_view,
            net['front_view']: batch_front_view,
            net['rgb_images']: batch_rgb_images,

            net['top_rois']:   batch_top_rois,
            net['front_rois']: batch_front_rois,
            net['rgb_rois']:   batch_rgb_rois,

            net['top_inds']:     batch_top_inds,
            net['top_pos_inds']: batch_top_pos_inds,
            net['top_labels']:   batch_top_labels,
            net['top_targets']:  batch_top_targets,

            net['fuse_labels']:  batch_fuse_labels,
            net['fuse_targets']: batch_fuse_targets,
        }

        t_cls_loss, t_reg_loss, f_cls_loss, f_reg_loss = \
           sess.run([top_cls_loss, top_reg_loss, fuse_cls_loss, fuse_reg_loss],fd2)


        # debug
        log_subdir = 'validation'
        top_image = data.draw_top_image(batch_top_view[0])
        rgb = batch_rgb_images[0]
        log_info_str = self.validation_set.get_frame_info(frame_id)[0]


        self.log_info(log_subdir,log_info_str)
        self.log_rpn(log_subdir, top_image, batch_gt_top_boxes, batch_gt_labels, batch_top_inds,
                     batch_top_labels,
                     batch_top_pos_inds, batch_top_targets, batch_proposals, batch_proposal_scores)

        self.log_fusion_net(log_subdir, top_image, batch_top_rois, batch_fuse_labels,
                            batch_fuse_targets, rgb, batch_rgb_rois)

        self.log_prediction(log_subdir, batch_proposals, batch_proposal_scores, fd1, batch_top_view,
                            batch_front_view,
                            batch_rgb_images, rgb, batch_gt_boxes3d, top_image)

        return t_cls_loss, t_reg_loss, f_cls_loss, f_reg_loss


    def train(self, max_iter=100000, pre_trained=True, train_set =None,validation_set =None):

        self.validation_set=validation_set
        #for init model
        top_shape, front_shape, rgb_shape = train_set.get_shape()


        net=mv3d_net.load(top_shape,front_shape,rgb_shape,self.num_class,len(self.bases))
        self.training_net=net
        top_cls_loss=net['top_cls_loss']
        top_reg_loss=net['top_reg_loss']
        fuse_cls_loss=net['fuse_cls_loss']
        fuse_reg_loss=net['fuse_reg_loss']

        # solver
        l2 = blocks.l2_regulariser(decay=0.0005)
        learning_rate = tf.placeholder(tf.float32, shape=[])
        solver = tf.train.MomentumOptimizer(learning_rate=learning_rate, momentum=0.9)

        solver_step = solver.minimize(top_cls_loss+0.01*top_reg_loss+0.1*fuse_cls_loss+0.01*fuse_reg_loss+l2)


        iter_debug=40
        batch_size=1

        # start training here  #########################################################################################
        self.log.write('epoch     iter    rate   |  top_cls_loss   reg_loss   |  fuse_cls_loss  reg_loss  total |  \n')
        self.log.write('-------------------------------------------------------------------------------------\n')

        self.training_sess = tf.Session()
        sess=self.training_sess
        saver = tf.train.Saver(keep_checkpoint_every_n_hours=0.2, max_to_keep=10)
        with sess.as_default():
            if pre_trained==True and not tf.train.latest_checkpoint(cfg.CHECKPOINT_DIR)==None:
                saver.restore(sess, tf.train.latest_checkpoint(cfg.CHECKPOINT_DIR))
            else:
                sess.run( tf.global_variables_initializer(), { blocks.IS_TRAIN_PHASE : True } )

            proposals=net['proposals']
            proposal_scores=net['proposal_scores']
            top_features=net['top_features']

            # set anchor boxes
            top_feature_shape=get_top_feature_shape(top_shape, self.stride)
            self.top_view_anchors, self.inside_inds = make_anchors(self.bases, self.stride, top_shape[0:2],top_feature_shape[0:2])
            self.inside_inds = np.arange(0, len(self.top_view_anchors), dtype=np.int32)  # use all  #<todo>

            if 0:
                summary_writer = tf.summary.FileWriter(os.path.join(cfg.LOG_DIR, 'graph'), sess.graph)
                summary_writer.close()

            loss_smooth_step=20
            ckpt_save_step=200

            rate = 0.01
            loss_sum = np.zeros(4)

            if cfg.TRAINING_TIMER:
                time_it = timer()

            for iter in range(max_iter):

                batch_rgb_images, batch_top_view, batch_front_view, \
                train_gt_labels, train_gt_boxes3d,frame_id = train_set.load(batch_size,shuffled=True)
                if self.batch_data_is_invalid(train_gt_boxes3d[0]):
                    continue
                top_image = data.draw_top_image(batch_top_view[0])
                top_image_bbox = box.draw_box3d_on_top(top_image, train_gt_boxes3d[0], color=(0, 0, 80))
                nud.imsave('top_image_dump', top_image_bbox, 'debug')

                epoch=1.0 * iter

                idx=iter%len(batch_rgb_images)

                batch_gt_labels    = train_gt_labels[0]
                batch_gt_boxes3d   = train_gt_boxes3d[0]
                batch_gt_top_boxes = data.box3d_to_top_box(batch_gt_boxes3d)


                ## run propsal generation
                fd1={
                    net['top_view']: batch_top_view,
                    net['top_anchors']:self.top_view_anchors,
                    net['top_inside_inds']: self.inside_inds,

                    learning_rate:   rate,
                    blocks.IS_TRAIN_PHASE:  True
                }
                batch_proposals, batch_proposal_scores, batch_top_features = \
                    sess.run([proposals, proposal_scores, top_features],fd1)

                ## generate  train rois  for RPN
                batch_top_inds, batch_top_pos_inds, batch_top_labels, batch_top_targets  = \
                    rpn_target ( self.top_view_anchors, self.inside_inds, batch_gt_labels,  batch_gt_top_boxes)

                batch_top_rois, batch_fuse_labels, batch_fuse_targets  = \
                     rcnn_target(  batch_proposals, batch_gt_labels, batch_gt_top_boxes, batch_gt_boxes3d )

                batch_3d_rois	 = project_to_roi3d    (batch_top_rois)
                batch_front_rois = project_to_front_roi(batch_3d_rois  )
                batch_rgb_rois   = project_to_rgb_roi  (batch_3d_rois  )


                ## run classification and regression loss -----------
                fd2={
                    **fd1,

                    net['top_view']: batch_top_view,
                    net['front_view']: batch_front_view,
                    net['rgb_images']: batch_rgb_images,

                    net['top_rois']:   batch_top_rois,
                    net['front_rois']: batch_front_rois,
                    net['rgb_rois']:   batch_rgb_rois,

                    net['top_inds']:     batch_top_inds,
                    net['top_pos_inds']: batch_top_pos_inds,
                    net['top_labels']:   batch_top_labels,
                    net['top_targets']:  batch_top_targets,

                    net['fuse_labels']:  batch_fuse_labels,
                    net['fuse_targets']: batch_fuse_targets,
                }

                _, t_cls_loss, t_reg_loss, f_cls_loss, f_reg_loss = \
                   sess.run([solver_step, top_cls_loss, top_reg_loss, fuse_cls_loss, fuse_reg_loss],fd2)

                loss_sum+= np.array([t_cls_loss, t_reg_loss, f_cls_loss, f_reg_loss])

                if iter%ckpt_save_step==0:
                    saver.save(sess, os.path.join(cfg.CHECKPOINT_DIR, 'mv3d_mode_snap.ckpt'))
                    if cfg.TRAINING_TIMER:
                        self.log.write('It takes %0.2f secs to train %d iterations. \n' %\
                                       (time_it.time_diff_per_n_loops(), ckpt_save_step))

                if iter%loss_smooth_step==0:
                    loss_smooth=loss_sum/loss_smooth_step
                    self.log.write('%3.1f   %d   %0.4f   |   %0.5f   %0.5f   |   %0.5f   %0.5f \n' %\
                        (epoch, idx, rate, loss_smooth[0], loss_smooth[1],loss_smooth[2], loss_smooth[3]))
                    loss_sum=0

                    va_top_cls_loss, va_top_reg_loss,va_fuse_cls_loss, va_fuse_reg_loss =\
                        self.validation_accuracy()
                    self.log.write('validation:         |   %0.5f   %0.5f   |   %0.5f   %0.5f \n\n' % \
                                   ( va_top_cls_loss, va_top_reg_loss, va_fuse_cls_loss, va_fuse_reg_loss))




                #debug: ------------------------------------
                if 1 and iter%iter_debug==0:
                    self.log_num+=1
                    self.log_num=self.log_num%self.log_max
                    log_info_str=train_set.get_frame_info(frame_id)[0]

                    log_subdir= str(self.log_num)
                    top_image = data.draw_top_image(batch_top_view[0])
                    rgb       = batch_rgb_images[idx]



                    self.log_info(log_subdir,log_info_str)
                    self.log_rpn(log_subdir, top_image, batch_gt_top_boxes, batch_gt_labels, batch_top_inds,
                            batch_top_labels,
                            batch_top_pos_inds, batch_top_targets, batch_proposals, batch_proposal_scores)

                    self.log_fusion_net(log_subdir ,top_image,batch_top_rois, batch_fuse_labels ,
                       batch_fuse_targets ,rgb, batch_rgb_rois)

                    self.log_prediction( log_subdir, batch_proposals, batch_proposal_scores, fd1, batch_top_view,
                                   batch_front_view,
                                   batch_rgb_images, rgb, batch_gt_boxes3d, top_image)
                    # latest
                    log_subdir='latest'
                    self.log_info(log_subdir,log_info_str)
                    self.log_rpn(log_subdir, top_image, batch_gt_top_boxes, batch_gt_labels, batch_top_inds,
                            batch_top_labels,
                            batch_top_pos_inds, batch_top_targets, batch_proposals, batch_proposal_scores)

                    self.log_fusion_net(log_subdir ,top_image,batch_top_rois, batch_fuse_labels ,
                       batch_fuse_targets ,rgb, batch_rgb_rois)

                    self.log_prediction( log_subdir, batch_proposals, batch_proposal_scores, fd1, batch_top_view,
                                   batch_front_view,
                                   batch_rgb_images, rgb, batch_gt_boxes3d, top_image)

            if cfg.TRAINING_TIMER:
                self.log.write('It takes %0.2f secs to train the dataset. \n' % \
                               (time_it.total_time()))


    def tracking_init(self,top_view_shape, front_view_shape, rgb_image_shape):
        # set anchor boxes
        top_feature_shape=get_top_feature_shape(top_view_shape, self.stride)
        self.top_view_anchors, self.inside_inds = make_anchors(self.bases, self.stride, top_view_shape[0:2],top_feature_shape[0:2])
        self.anchors_inside_inds = np.arange(0, len(self.top_view_anchors), dtype=np.int32)  # use all  #<todo>

        self.net = mv3d_net.load(top_view_shape, front_view_shape, rgb_image_shape, self.num_class, len(self.bases))

        self.tracking_sess = tf.Session()
        saver = tf.train.Saver()
        saver.restore(self.tracking_sess,tf.train.latest_checkpoint(cfg.CHECKPOINT_DIR))



    def tacking(self, top_view, front_view, rgb_image,top_image=None):
        lables=[] #todo add lables output
        np_reshape = lambda np_array: np_array.reshape(1, *(np_array.shape))
        top_view=np_reshape(top_view)
        front_view=np_reshape(front_view)
        rgb_image=np_reshape(rgb_image)


        fd1 = {
            self.net['top_view']: top_view,
            self.net['top_anchors']: self.top_view_anchors,
            self.net['top_inside_inds']: self.anchors_inside_inds,
            blocks.IS_TRAIN_PHASE: False
        }

        top_view_proposals, batch_proposal_scores = \
            self.tracking_sess.run([self.net['proposals'], self.net['proposal_scores']], fd1)

        top_rois=top_view_proposals[batch_proposal_scores>0.1,:]
        if len(top_rois)==0:
            return np.zeros((0,8,3)), []

        rois3d = project_to_roi3d(top_rois)
        front_rois = project_to_front_roi(rois3d)
        rgb_rois = project_to_rgb_roi(rois3d)


        fd2 = {
            **fd1,
            self.net['front_view']: front_view,
            self.net['rgb_images']: rgb_image,

            self.net['top_rois']: top_rois,
            self.net['front_rois']: front_rois,
            self.net['rgb_rois']: rgb_rois,

        }

        fuse_probs, fuse_deltas = \
            self.tracking_sess.run([ self.net['fuse_probs'], self.net['fuse_deltas'] ],fd2)

        probs, boxes3d = rcnn_nms(fuse_probs, fuse_deltas, rois3d, score_threshold=0.1)

        # #debug
        # predicted_bbox = nud.draw_boxed3d_to_rgb(rgb_image[0], boxes3d)
        # nud.imsave('predicted_bbox',predicted_bbox)
        return boxes3d,lables